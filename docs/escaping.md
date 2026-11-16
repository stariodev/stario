# How Stario writes HTML

## The rule

Stario keeps every value in its slot. User text in `h.P(name)` cannot become a tag. A value in `{"title": name}` cannot end its quotes and add an attribute. You never escape by hand.

The key you write decides what the value means. `"title"` is text. `"onclick"` is JavaScript, `"style"` is CSS and `"data-show"` is a Datastar expression. Stario writes the value safely into its slot. It does not guess what you meant. Never build a key from user data.

There is one check on values: a URL attribute refuses `javascript:`. For a URL, the danger is in the value, not in the key.

## Where a value goes

| You write | What Stario does | Why |
|---|---|---|
| `h.P(name)` | Escapes `&` `<` `>`. | The parser reads `<` as a tag and `&` as an entity. Text in, the same text out. |
| `h.Div({"title": name})` | Writes `"…"` and escapes `&` `<` `>` `"`. | A `"` would end the value. A `<` is escaped too, because inside `<noscript>` the parser can read `</noscript` in an attribute. |
| `h.A({"href": url})`, and `src`, `action`, `formaction`, `xlink:href` | Escapes like any attribute. Raises on a `javascript:` URL. | A click on a `javascript:` link runs code as your site. Stario reads the scheme as a browser does: upper case, spaces and controls in front, and tabs inside all count. Other schemes pass: `mailto:`, `tel:`, app links and `<a download href="data:…">`. |
| `styles({"width": f"{pct}%"})` | Writes the declarations. Raises when a value has `; { } \`, or an unclosed quote, `(`, `[` or `/*`. | Without the check, a value could end its declaration or take the next ones: an unclosed `url(` would take `;display:none` into the URL. |
| `h.Script(code)`, `h.Style(css)` | Writes the code as it is. Raises on `</script` (and `<!--` in a script) or `</style`. | The parser decodes nothing here, so escaping would break the code. The string is code you wrote. |
| `h.Script(dumps_script(data))` | JSON with `<` `>` `&` escaped. | User data cannot end the script, and the JSON stays valid. |
| `{"disabled": True}` | `True` writes the bare name. `False` and `None` leave it out. | This is the one rule for every attribute. An attribute that takes words gets the word as a string: `{"aria-expanded": "false"}`, `{"draggable": "true"}`, `{"autocomplete": "off"}`. |

## Code attributes

An `on*` value is JavaScript. To put a Python value in it, use `literal()`. It quotes the value as a JavaScript literal, with or without Datastar:

```python
h.Button({"onclick": f"pick({literal(name)})"}, "Pick")
```

HTML escaping alone does not make user text safe inside JavaScript.

## Datastar

In `data.*` and `at.*`, a `str` argument is a Datastar expression, which is code. The values that Datastar stores or sends are data, and Stario quotes them. That covers URLs, selectors, `headers=`, `data.signals` and a `payload=` dict. To put a Python value into code, use `literal(v)`.

```python
data.on("click", f"$tags = $tags.filter(t => t !== {literal(tag)})")
at.post(SAVE.href(), payload={"title": title})               # data: the text
at.post(SAVE.href(), payload="{id: $id, title: el.value}")   # one expression
```

Datastar runs every attribute with one of its names, also one that you write in a mapping or with `markup.data()`. For example, `data({"text": label})` writes `data-text`, and Datastar runs the label as code. Use other names for plain data, such as `data-label` and `data-testid`.

## Responses

- `w.html(view)` and `sse.patch_elements(view)` take `h.*` markup, a `SafeString` or `bytes`. A plain `str` raises, because a `str` is text everywhere in Stario. Finished HTML from a template engine is `w.html(SafeString(page))`.
- `w.text(s)` and `w.json(v)` send `text/plain` and `application/json`. The browser does not read them as HTML.
- `w.redirect(url)` and `sse.navigate(url)` refuse line breaks, `\` and schemes other than `http(s)`. A line break would add a header.

## Finished bytes

- `SafeString(s)` means "these characters are finished". Stario skips the escaping and the URL check. An attribute value still cannot end its quotes. Use it for template output, or a `javascript:void(0)` that you wrote.
- `Attrs(" name=…")` is finished attribute bytes, written as they are. Every `data.*` helper returns one.
- `bytes` is a finished response body.

Never put user data in these three.

## Rich text from users

Stario does not clean HTML. To show user HTML (comments, Markdown output), clean it with a sanitizer such as `nh3`, then wrap it in `SafeString`. The sanitizer must not allow `on*` or `data-*` attributes, because Datastar runs `data-*`.

## An iframe page

`srcdoc` is a whole HTML page that runs with your site's origin. Render the page, then pass the string. Stario escapes it into the attribute, and the user text stays text inside:

```python
h.Iframe({"srcdoc": render(h.P(comment))})
```

## What Stario cannot check

- A URL that arrives through a signal: `data.attr("href", "$url")`. Datastar sets it in the browser. Check the URL on the server before you put it in a signal.
- An `id` or `name` that users choose. A form or image named `currentScript` hides `document.currentScript`. Prefix such ids with your own text.
- A `<style>` or `<script>` inside `svg.Svg(...)`. There the parser reads markup, not raw text. Use `svg.Style` and `svg.Script`.
- An attribute name that you write twice. The browser keeps the first one.

## Content Security Policy

By default, Datastar compiles expressions with `Function()`, so a CSP needs `'unsafe-eval'`. Datastar 1.0.5 also has a nonce mode. Put `data-nonce="<nonce>"` on `<html>`, and send `script-src 'self' 'nonce-<nonce>'`. The nonce must be new for each response.

A CSP blocks `on*` attributes, `javascript:` URLs and injected `<script>` elements. It does not stop an injected `data-*` expression, because Datastar runs it. Neither does the nonce mode: Datastar gives the page nonce to scripts in patched HTML. Your first defence is to keep user data in text and attribute slots.
