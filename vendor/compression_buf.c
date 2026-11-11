#include "compression_buf.h"

#include <brotli/decode.h>
#include <brotli/encode.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <zlib.h>
#include <zstd.h>

#ifndef STARIO_BROTLI_HTTP_WINDOW
#define STARIO_BROTLI_HTTP_WINDOW 18
#endif

struct StarioBrotli {
    BrotliEncoderState* state;
    unsigned char* out;
    size_t out_cap;
    int finished;
    int level;
    int window_log;
    int lgblock;
};

struct StarioGzip {
    z_stream strm;
    unsigned char* out;
    size_t out_cap;
    int finished;
    int level;
    int window_bits;
    int mem_level;
};

struct StarioZstd {
    ZSTD_CCtx* cctx;
    unsigned char* out;
    size_t out_cap;
    int finished;
    int level;
    int window_log;
    int hash_log;
    int chain_log;
};

#define STARIO_CODEC_POOL_MAX 32
#define STARIO_RETAINED_OUTPUT_MAX (64 * 1024)

#if defined(_MSC_VER)
#define STARIO_TLS __declspec(thread)
#else
#define STARIO_TLS _Thread_local
#endif

/* Per-OS-thread pools: connection affinity never shares an encoder, and
   free-threaded workers must not race a process-global free-list. */
static STARIO_TLS StarioBrotli* brotli_pool[STARIO_CODEC_POOL_MAX];
static STARIO_TLS size_t brotli_pool_count = 0;
static STARIO_TLS StarioGzip* gzip_pool[STARIO_CODEC_POOL_MAX];
static STARIO_TLS size_t gzip_pool_count = 0;
static STARIO_TLS StarioZstd* zstd_pool[STARIO_CODEC_POOL_MAX];
static STARIO_TLS size_t zstd_pool_count = 0;

static void trim_output(unsigned char** out, size_t* cap) {
    if (*cap > STARIO_RETAINED_OUTPUT_MAX) {
        free(*out);
        *out = NULL;
        *cap = 0;
    }
}

static int grow_buffer(unsigned char** buf, size_t* cap, size_t used) {
    size_t next_cap;
    unsigned char* next;

    if (*cap > SIZE_MAX / 2) {
        return -1;
    }
    next_cap = *cap < 256 ? 256 : *cap * 2;
    if (next_cap <= used) {
        if (used == SIZE_MAX) {
            return -1;
        }
        next_cap = used + 1;
    }
    next = (unsigned char*)realloc(*buf, next_cap);
    if (next == NULL) {
        return -1;
    }
    *buf = next;
    *cap = next_cap;
    return 0;
}

static int brotli_append(
    StarioBrotli* brotli,
    BrotliEncoderOperation operation,
    const unsigned char* in,
    size_t in_len,
    size_t* used
) {
    size_t available_in = in_len;
    size_t available_out;
    size_t previous_in;
    size_t previous_used;
    const unsigned char* next_in = in;
    unsigned char* next_out;

    for (;;) {
        if (
            *used == brotli->out_cap &&
            grow_buffer(&brotli->out, &brotli->out_cap, *used) != 0
        ) {
            return -1;
        }
        available_out = brotli->out_cap - *used;
        next_out = brotli->out + *used;
        previous_in = available_in;
        previous_used = *used;
        if (!BrotliEncoderCompressStream(
            brotli->state,
            operation,
            &available_in,
            &next_in,
            &available_out,
            &next_out,
            NULL
        )) {
            return -1;
        }
        *used = (size_t)(next_out - brotli->out);

        if (operation == BROTLI_OPERATION_FINISH) {
            if (BrotliEncoderIsFinished(brotli->state)) {
                brotli->finished = 1;
                break;
            }
        } else if (
            available_in == 0 &&
            !BrotliEncoderHasMoreOutput(brotli->state)
        ) {
            break;
        }

        if (available_in == previous_in && *used == previous_used) {
            return -1;
        }
    }
    return 0;
}

static int brotli_emit(
    StarioBrotli* brotli,
    BrotliEncoderOperation first,
    BrotliEncoderOperation flush,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    size_t used = 0;

    if (
        brotli == NULL || brotli->state == NULL || brotli->finished ||
        out == NULL || out_len == NULL ||
        (in == NULL && in_len != 0)
    ) {
        return -1;
    }
    *out = NULL;
    *out_len = 0;
    if (brotli_append(brotli, first, in, in_len, &used) != 0) {
        return -1;
    }
    if (flush != first && brotli_append(brotli, flush, NULL, 0, &used) != 0) {
        return -1;
    }
    *out = brotli->out;
    *out_len = used;
    return 0;
}

static int brotli_start(
    StarioBrotli* brotli, int level, int window_log, int lgblock
) {
    uint32_t lgwin;
    brotli->state = BrotliEncoderCreateInstance(NULL, NULL, NULL);
    brotli->finished = 0;
    if (brotli->state == NULL) {
        return -1;
    }
    lgwin = window_log > 0 ? (uint32_t)window_log : STARIO_BROTLI_HTTP_WINDOW;
    if (
        !BrotliEncoderSetParameter(
            brotli->state, BROTLI_PARAM_QUALITY, (uint32_t)level
        ) ||
        !BrotliEncoderSetParameter(brotli->state, BROTLI_PARAM_LGWIN, lgwin) ||
        (lgblock > 0 &&
         !BrotliEncoderSetParameter(
             brotli->state, BROTLI_PARAM_LGBLOCK, (uint32_t)lgblock
         ))
    ) {
        BrotliEncoderDestroyInstance(brotli->state);
        brotli->state = NULL;
        return -1;
    }
    brotli->level = level;
    brotli->window_log = (int)lgwin;
    brotli->lgblock = lgblock;
    return 0;
}

StarioBrotli* stario_brotli_new(int level, int window_log, int lgblock) {
    StarioBrotli* brotli = (StarioBrotli*)calloc(1, sizeof(StarioBrotli));
    if (brotli == NULL || brotli_start(brotli, level, window_log, lgblock) != 0) {
        free(brotli);
        return NULL;
    }
    return brotli;
}

StarioBrotli* stario_brotli_acquire(int level, int window_log, int lgblock) {
    StarioBrotli* brotli;
    int resolved_window = window_log > 0 ? window_log : STARIO_BROTLI_HTTP_WINDOW;
    size_t i;
    for (i = 0; i < brotli_pool_count; i++) {
        brotli = brotli_pool[i];
        if (
            brotli->level == level && brotli->window_log == resolved_window &&
            brotli->lgblock == lgblock
        ) {
            brotli_pool[i] = brotli_pool[--brotli_pool_count];
            if (brotli_start(brotli, level, resolved_window, lgblock) == 0) {
                return brotli;
            }
            stario_brotli_free(brotli);
            return NULL;
        }
    }
    return stario_brotli_new(level, resolved_window, lgblock);
}

int stario_brotli_block_borrowed(
    StarioBrotli* brotli,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    return brotli_emit(
        brotli,
        BROTLI_OPERATION_PROCESS,
        BROTLI_OPERATION_FLUSH,
        in,
        in_len,
        out,
        out_len
    );
}

int stario_brotli_finish_borrowed(
    StarioBrotli* brotli,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    return brotli_emit(
        brotli,
        BROTLI_OPERATION_FINISH,
        BROTLI_OPERATION_FINISH,
        in,
        in_len,
        out,
        out_len
    );
}

void stario_brotli_free(StarioBrotli* brotli) {
    if (brotli == NULL) {
        return;
    }
    if (brotli->state != NULL) {
        BrotliEncoderDestroyInstance(brotli->state);
    }
    free(brotli->out);
    free(brotli);
}

/* One-shot output for a complete body. Brotli has no encoder reset, so a
   pooled stream encoder is created and destroyed per response anyway; the
   one-shot API skips the stream state entirely and writes straight into
   this per-thread scratch buffer. Valid until the next one-shot call. */
static STARIO_TLS unsigned char* brotli_oneshot_scratch = NULL;
static STARIO_TLS size_t brotli_oneshot_scratch_cap = 0;

int stario_brotli_compress_borrowed(
    int level,
    int window_log,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    size_t need;
    size_t encoded;
    int lgwin = window_log > 0 ? window_log : STARIO_BROTLI_HTTP_WINDOW;

    if (out == NULL || out_len == NULL || (in == NULL && in_len != 0)) {
        return -1;
    }
    *out = NULL;
    *out_len = 0;
    need = BrotliEncoderMaxCompressedSize(in_len);
    if (need == 0) {
        return -1;
    }
    /* Grow to fit; shrink again once a huge body has left so one large
       response does not pin its buffer on this thread for good. */
    if (
        brotli_oneshot_scratch_cap < need ||
        (brotli_oneshot_scratch_cap > STARIO_RETAINED_OUTPUT_MAX * 4 &&
         need <= STARIO_RETAINED_OUTPUT_MAX)
    ) {
        size_t cap = need < 4096 ? 4096 : need;
        unsigned char* next = (unsigned char*)realloc(brotli_oneshot_scratch, cap);
        if (next == NULL) {
            return -1;
        }
        brotli_oneshot_scratch = next;
        brotli_oneshot_scratch_cap = cap;
    }
    encoded = brotli_oneshot_scratch_cap;
    if (!BrotliEncoderCompress(
        level,
        lgwin,
        BROTLI_DEFAULT_MODE,
        in_len,
        in,
        &encoded,
        brotli_oneshot_scratch
    )) {
        return -1;
    }
    *out = brotli_oneshot_scratch;
    *out_len = encoded;
    return 0;
}

void stario_brotli_release(StarioBrotli* brotli) {
    if (brotli == NULL) {
        return;
    }
    if (brotli->state != NULL) {
        BrotliEncoderDestroyInstance(brotli->state);
        brotli->state = NULL;
    }
    brotli->finished = 0;
    trim_output(&brotli->out, &brotli->out_cap);
    if (brotli_pool_count < STARIO_CODEC_POOL_MAX) {
        brotli_pool[brotli_pool_count++] = brotli;
    } else {
        stario_brotli_free(brotli);
    }
}

static int gzip_deflate(
    StarioGzip* gzip,
    int flush,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    size_t used = 0;
    size_t remaining = in_len;
    size_t room;
    uInt take;
    int rc;

    if (
        gzip == NULL || gzip->finished ||
        out == NULL || out_len == NULL ||
        (in == NULL && in_len != 0)
    ) {
        return -1;
    }
    *out = NULL;
    *out_len = 0;
    /* zlib counts in uInt: feed inputs over 4 GiB in slices. */
    gzip->strm.next_in = (Bytef*)in;
    gzip->strm.avail_in = 0;
    for (;;) {
        if (gzip->strm.avail_in == 0 && remaining > 0) {
            take = remaining > UINT_MAX ? UINT_MAX : (uInt)remaining;
            gzip->strm.avail_in = take;
            remaining -= take;
        }
        if (
            used == gzip->out_cap &&
            grow_buffer(&gzip->out, &gzip->out_cap, used) != 0
        ) {
            return -1;
        }
        room = gzip->out_cap - used;
        gzip->strm.next_out = gzip->out + used;
        gzip->strm.avail_out = room > UINT_MAX ? UINT_MAX : (uInt)room;
        rc = deflate(&gzip->strm, remaining > 0 ? Z_NO_FLUSH : flush);
        used = (size_t)(gzip->strm.next_out - gzip->out);
        if (rc == Z_STREAM_END) {
            gzip->finished = 1;
            break;
        }
        if (rc != Z_OK) {
            return -1;
        }
        if (
            gzip->strm.avail_in == 0 && remaining == 0 &&
            gzip->strm.avail_out > 0
        ) {
            break;
        }
    }
    *out = gzip->out;
    *out_len = used;
    return 0;
}

StarioGzip* stario_gzip_new(int level, int window_bits, int mem_level) {
    StarioGzip* gzip = (StarioGzip*)malloc(sizeof(StarioGzip));
    int resolved_mem = mem_level > 0 ? mem_level : 8;

    if (gzip == NULL) {
        return NULL;
    }
    memset(&gzip->strm, 0, sizeof(gzip->strm));
    if (deflateInit2(
        &gzip->strm,
        level,
        Z_DEFLATED,
        16 + window_bits,
        resolved_mem,
        Z_DEFAULT_STRATEGY
    ) != Z_OK) {
        free(gzip);
        return NULL;
    }
    gzip->out = NULL;
    gzip->out_cap = 0;
    gzip->finished = 0;
    gzip->level = level;
    gzip->window_bits = window_bits;
    gzip->mem_level = resolved_mem;
    return gzip;
}

StarioGzip* stario_gzip_acquire(int level, int window_bits, int mem_level) {
    StarioGzip* gzip;
    int resolved_mem = mem_level > 0 ? mem_level : 8;
    size_t i;
    for (i = 0; i < gzip_pool_count; i++) {
        gzip = gzip_pool[i];
        if (
            gzip->level == level && gzip->window_bits == window_bits &&
            gzip->mem_level == resolved_mem
        ) {
            gzip_pool[i] = gzip_pool[--gzip_pool_count];
            if (deflateReset(&gzip->strm) != Z_OK) {
                stario_gzip_free(gzip);
                return NULL;
            }
            gzip->finished = 0;
            return gzip;
        }
    }
    return stario_gzip_new(level, window_bits, resolved_mem);
}

int stario_gzip_block_borrowed(
    StarioGzip* gzip,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    return gzip_deflate(gzip, Z_SYNC_FLUSH, in, in_len, out, out_len);
}

int stario_gzip_finish_borrowed(
    StarioGzip* gzip,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    return gzip_deflate(gzip, Z_FINISH, in, in_len, out, out_len);
}

void stario_gzip_free(StarioGzip* gzip) {
    if (gzip == NULL) {
        return;
    }
    deflateEnd(&gzip->strm);
    free(gzip->out);
    free(gzip);
}

void stario_gzip_release(StarioGzip* gzip) {
    if (gzip == NULL) {
        return;
    }
    trim_output(&gzip->out, &gzip->out_cap);
    if (gzip_pool_count < STARIO_CODEC_POOL_MAX) {
        gzip_pool[gzip_pool_count++] = gzip;
    } else {
        stario_gzip_free(gzip);
    }
}

static int zstd_start(
    StarioZstd* zstd, int level, int window_log, int hash_log, int chain_log
) {
    if (zstd->cctx == NULL) {
        zstd->cctx = ZSTD_createCCtx();
        if (zstd->cctx == NULL) {
            return -1;
        }
    } else if (ZSTD_isError(
        ZSTD_CCtx_reset(zstd->cctx, ZSTD_reset_session_and_parameters)
    )) {
        return -1;
    }
    zstd->finished = 0;
    if (ZSTD_isError(
        ZSTD_CCtx_setParameter(zstd->cctx, ZSTD_c_compressionLevel, level)
    )) {
        return -1;
    }
    /* 0 keeps libzstd's default for each parameter. */
    if (
        (window_log > 0 &&
         ZSTD_isError(
             ZSTD_CCtx_setParameter(zstd->cctx, ZSTD_c_windowLog, window_log)
         )) ||
        (hash_log > 0 &&
         ZSTD_isError(
             ZSTD_CCtx_setParameter(zstd->cctx, ZSTD_c_hashLog, hash_log)
         )) ||
        (chain_log > 0 &&
         ZSTD_isError(
             ZSTD_CCtx_setParameter(zstd->cctx, ZSTD_c_chainLog, chain_log)
         ))
    ) {
        return -1;
    }
    zstd->level = level;
    zstd->window_log = window_log;
    zstd->hash_log = hash_log;
    zstd->chain_log = chain_log;
    return 0;
}

static int zstd_emit(
    StarioZstd* zstd,
    ZSTD_EndDirective end_op,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    ZSTD_inBuffer input;
    ZSTD_outBuffer output;
    size_t used = 0;
    size_t remaining;
    size_t previous_in;
    size_t previous_used;

    if (
        zstd == NULL || zstd->cctx == NULL || zstd->finished ||
        out == NULL || out_len == NULL ||
        (in == NULL && in_len != 0)
    ) {
        return -1;
    }
    *out = NULL;
    *out_len = 0;

    input.src = in;
    input.size = in_len;
    input.pos = 0;

    for (;;) {
        if (
            used == zstd->out_cap &&
            grow_buffer(&zstd->out, &zstd->out_cap, used) != 0
        ) {
            return -1;
        }
        output.dst = zstd->out;
        output.size = zstd->out_cap;
        output.pos = used;
        previous_in = input.pos;
        previous_used = used;
        remaining = ZSTD_compressStream2(zstd->cctx, &output, &input, end_op);
        if (ZSTD_isError(remaining)) {
            return -1;
        }
        used = output.pos;
        if (end_op == ZSTD_e_end) {
            if (remaining == 0) {
                zstd->finished = 1;
                break;
            }
        } else if (input.pos == input.size && remaining == 0) {
            break;
        }
        if (input.pos == previous_in && used == previous_used) {
            return -1;
        }
    }
    *out = zstd->out;
    *out_len = used;
    return 0;
}

StarioZstd* stario_zstd_new(int level, int window_log, int hash_log, int chain_log) {
    StarioZstd* zstd = (StarioZstd*)calloc(1, sizeof(StarioZstd));
    if (zstd == NULL || zstd_start(zstd, level, window_log, hash_log, chain_log) != 0) {
        if (zstd != NULL) {
            ZSTD_freeCCtx(zstd->cctx);
        }
        free(zstd);
        return NULL;
    }
    return zstd;
}

StarioZstd* stario_zstd_acquire(int level, int window_log, int hash_log, int chain_log) {
    StarioZstd* zstd;
    size_t i;
    for (i = 0; i < zstd_pool_count; i++) {
        zstd = zstd_pool[i];
        if (
            zstd->level == level && zstd->window_log == window_log &&
            zstd->hash_log == hash_log && zstd->chain_log == chain_log
        ) {
            zstd_pool[i] = zstd_pool[--zstd_pool_count];
            if (
                zstd->cctx != NULL &&
                !ZSTD_isError(
                    ZSTD_CCtx_reset(zstd->cctx, ZSTD_reset_session_only)
                )
            ) {
                zstd->finished = 0;
                return zstd;
            }
            stario_zstd_free(zstd);
            return NULL;
        }
    }
    return stario_zstd_new(level, window_log, hash_log, chain_log);
}

int stario_zstd_block_borrowed(
    StarioZstd* zstd,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    return zstd_emit(zstd, ZSTD_e_flush, in, in_len, out, out_len);
}

int stario_zstd_finish_borrowed(
    StarioZstd* zstd,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    return zstd_emit(zstd, ZSTD_e_end, in, in_len, out, out_len);
}

void stario_zstd_free(StarioZstd* zstd) {
    if (zstd == NULL) {
        return;
    }
    ZSTD_freeCCtx(zstd->cctx);
    free(zstd->out);
    free(zstd);
}

void stario_zstd_release(StarioZstd* zstd) {
    if (zstd == NULL) {
        return;
    }
    trim_output(&zstd->out, &zstd->out_cap);
    if (zstd_pool_count < STARIO_CODEC_POOL_MAX) {
        zstd_pool[zstd_pool_count++] = zstd;
    } else {
        stario_zstd_free(zstd);
    }
}

struct StarioBrotliDecoder {
    BrotliDecoderState* state;
    unsigned char* out;
    size_t out_cap;
    int finished;
};

struct StarioZstdDecoder {
    ZSTD_DCtx* dctx;
    unsigned char* out;
    size_t out_cap;
    int finished;
};

static STARIO_TLS unsigned char* brotli_dec_scratch = NULL;
static STARIO_TLS size_t brotli_dec_scratch_cap = 0;
static STARIO_TLS unsigned char* zstd_dec_scratch = NULL;
static STARIO_TLS size_t zstd_dec_scratch_cap = 0;

static int brotli_decode_into(
    BrotliDecoderState* state,
    unsigned char** buf,
    size_t* cap,
    const unsigned char* in,
    size_t in_len,
    size_t* used,
    int* finished
) {
    size_t available_in = in_len;
    const unsigned char* next_in = in;
    BrotliDecoderResult rc;

    *used = 0;
    for (;;) {
        size_t available_out;
        unsigned char* next_out;
        if (
            *used == *cap &&
            grow_buffer(buf, cap, *used) != 0
        ) {
            return -1;
        }
        available_out = *cap - *used;
        next_out = *buf + *used;
        rc = BrotliDecoderDecompressStream(
            state,
            &available_in,
            &next_in,
            &available_out,
            &next_out,
            NULL
        );
        *used = (size_t)(next_out - *buf);
        if (rc == BROTLI_DECODER_RESULT_SUCCESS) {
            *finished = 1;
            /* Bytes after the last meta-block are not part of the stream;
               the reference decoder rejects them too. */
            return available_in == 0 ? 0 : -1;
        }
        if (rc == BROTLI_DECODER_RESULT_NEEDS_MORE_INPUT) {
            return 0;
        }
        if (rc == BROTLI_DECODER_RESULT_NEEDS_MORE_OUTPUT) {
            if (grow_buffer(buf, cap, *used) != 0) {
                return -1;
            }
            continue;
        }
        return -1;
    }
}

static int zstd_decode_into(
    ZSTD_DCtx* dctx,
    unsigned char** buf,
    size_t* cap,
    const unsigned char* in,
    size_t in_len,
    size_t* used,
    int* finished
) {
    ZSTD_inBuffer input;
    ZSTD_outBuffer output;
    size_t remaining;

    input.src = in;
    input.size = in_len;
    input.pos = 0;
    *used = 0;
    /* A zstd stream is any number of frames (RFC 8878 3): finished means
       the input ended exactly at the end of a frame. */
    if (in_len > 0) {
        *finished = 0;
    }
    for (;;) {
        if (
            *used == *cap &&
            grow_buffer(buf, cap, *used) != 0
        ) {
            return -1;
        }
        output.dst = *buf;
        output.size = *cap;
        output.pos = *used;
        remaining = ZSTD_decompressStream(dctx, &output, &input);
        if (ZSTD_isError(remaining)) {
            return -1;
        }
        *used = output.pos;
        if (remaining == 0) {
            /* Frame complete and flushed; the next byte starts a new frame. */
            if (input.pos == input.size) {
                *finished = 1;
                return 0;
            }
            continue;
        }
        if (input.pos == input.size && output.pos < output.size) {
            return 0;
        }
        if (output.pos == output.size && grow_buffer(buf, cap, *used) != 0) {
            return -1;
        }
    }
}

uint32_t stario_brotli_decoder_version(void) {
    return BrotliDecoderVersion();
}

int stario_brotli_decompress_borrowed(
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    BrotliDecoderState* state;
    size_t used = 0;
    int finished = 0;

    if (out == NULL || out_len == NULL || (in == NULL && in_len != 0)) {
        return -1;
    }
    *out = NULL;
    *out_len = 0;
    state = BrotliDecoderCreateInstance(NULL, NULL, NULL);
    if (state == NULL) {
        return -1;
    }
    if (
        brotli_decode_into(
            state,
            &brotli_dec_scratch,
            &brotli_dec_scratch_cap,
            in,
            in_len,
            &used,
            &finished
        ) != 0 ||
        !finished
    ) {
        BrotliDecoderDestroyInstance(state);
        return -1;
    }
    BrotliDecoderDestroyInstance(state);
    *out = brotli_dec_scratch;
    *out_len = used;
    return 0;
}

int stario_zstd_decompress_borrowed(
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    ZSTD_DCtx* dctx;
    size_t used = 0;
    int finished = 0;

    if (out == NULL || out_len == NULL || (in == NULL && in_len != 0)) {
        return -1;
    }
    *out = NULL;
    *out_len = 0;
    dctx = ZSTD_createDCtx();
    if (dctx == NULL) {
        return -1;
    }
    if (
        zstd_decode_into(
            dctx,
            &zstd_dec_scratch,
            &zstd_dec_scratch_cap,
            in,
            in_len,
            &used,
            &finished
        ) != 0 ||
        !finished
    ) {
        ZSTD_freeDCtx(dctx);
        return -1;
    }
    ZSTD_freeDCtx(dctx);
    *out = zstd_dec_scratch;
    *out_len = used;
    return 0;
}

StarioBrotliDecoder* stario_brotli_decoder_new(void) {
    StarioBrotliDecoder* decoder = (StarioBrotliDecoder*)calloc(
        1, sizeof(StarioBrotliDecoder)
    );
    if (decoder == NULL) {
        return NULL;
    }
    decoder->state = BrotliDecoderCreateInstance(NULL, NULL, NULL);
    if (decoder->state == NULL) {
        free(decoder);
        return NULL;
    }
    return decoder;
}

int stario_brotli_decoder_push_borrowed(
    StarioBrotliDecoder* decoder,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    size_t used = 0;

    if (
        decoder == NULL || decoder->state == NULL ||
        out == NULL || out_len == NULL ||
        (in == NULL && in_len != 0)
    ) {
        return -1;
    }
    *out = NULL;
    *out_len = 0;
    if (
        brotli_decode_into(
            decoder->state,
            &decoder->out,
            &decoder->out_cap,
            in,
            in_len,
            &used,
            &decoder->finished
        ) != 0
    ) {
        return -1;
    }
    *out = decoder->out;
    *out_len = used;
    return 0;
}

int stario_brotli_decoder_finished(const StarioBrotliDecoder* decoder) {
    return decoder != NULL && decoder->finished;
}

void stario_brotli_decoder_free(StarioBrotliDecoder* decoder) {
    if (decoder == NULL) {
        return;
    }
    if (decoder->state != NULL) {
        BrotliDecoderDestroyInstance(decoder->state);
    }
    free(decoder->out);
    free(decoder);
}

StarioZstdDecoder* stario_zstd_decoder_new(void) {
    StarioZstdDecoder* decoder = (StarioZstdDecoder*)calloc(
        1, sizeof(StarioZstdDecoder)
    );
    if (decoder == NULL) {
        return NULL;
    }
    decoder->dctx = ZSTD_createDCtx();
    if (decoder->dctx == NULL) {
        free(decoder);
        return NULL;
    }
    return decoder;
}

int stario_zstd_decoder_push_borrowed(
    StarioZstdDecoder* decoder,
    const unsigned char* in,
    size_t in_len,
    const unsigned char** out,
    size_t* out_len
) {
    size_t used = 0;

    if (
        decoder == NULL || decoder->dctx == NULL ||
        out == NULL || out_len == NULL ||
        (in == NULL && in_len != 0)
    ) {
        return -1;
    }
    *out = NULL;
    *out_len = 0;
    if (
        zstd_decode_into(
            decoder->dctx,
            &decoder->out,
            &decoder->out_cap,
            in,
            in_len,
            &used,
            &decoder->finished
        ) != 0
    ) {
        return -1;
    }
    *out = decoder->out;
    *out_len = used;
    return 0;
}

int stario_zstd_decoder_finished(const StarioZstdDecoder* decoder) {
    return decoder != NULL && decoder->finished;
}

void stario_zstd_decoder_free(StarioZstdDecoder* decoder) {
    if (decoder == NULL) {
        return;
    }
    ZSTD_freeDCtx(decoder->dctx);
    free(decoder->out);
    free(decoder);
}
