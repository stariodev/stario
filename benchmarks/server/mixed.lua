-- Browser-shaped request mix: 40% /page, 30% /user/{id}, 20% /json, 10% POST /validate.
-- Every request carries a real browser's headers and a session cookie.
-- Accept-Encoding is a browser's; servers without compression ignore it.
-- wrk.format() with a custom table does not add Host, so it is set here
-- (Stario answers a Host-less HTTP/1.1 request with 400, as RFC 9112 asks).
local browser = {
  ["Host"] = "localhost",
  ["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
  ["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
  ["Accept-Language"] = "en-US,en;q=0.9,pl;q=0.8",
  ["Accept-Encoding"] = "gzip, deflate, br, zstd",
  ["Sec-Ch-Ua"] = '"Chromium";v="131", "Not_A Brand";v="24"',
  ["Sec-Ch-Ua-Mobile"] = "?0",
  ["Sec-Ch-Ua-Platform"] = '"Linux"',
  ["Sec-Fetch-Site"] = "same-origin",
  ["Sec-Fetch-Mode"] = "navigate",
  ["Sec-Fetch-Dest"] = "document",
  ["Referer"] = "http://localhost/products",
  ["Cookie"] = "session=9f2c1e7b4a6d8f0e3b5c7a9d1f3e5b7c; theme=dark; _ga=GA1.1.123456789.1700000000",
}

local post = {}
for k, v in pairs(browser) do post[k] = v end
post["Content-Type"] = "application/json"
post["Accept"] = "application/json"

local body = '{"name":"Ada","age":42}'
local requests = {}

local function add(n, req)
  for _ = 1, n do requests[#requests + 1] = req end
end

add(4, wrk.format("GET", "/page", browser))
add(1, wrk.format("GET", "/user/7", browser))
add(1, wrk.format("GET", "/user/42", browser))
add(1, wrk.format("GET", "/user/1234", browser))
add(2, wrk.format("GET", "/json", browser))
add(1, wrk.format("POST", "/validate", post, body))

local i = 0
request = function()
  i = i % #requests + 1
  return requests[i]
end
