# URL shortener service

As a user I want to turn a long URL into a short code and share the short link, so that long links are easy to send and I can see how often they are used.

## Details
- `POST /shorten` accepts `{"url": "<http or https URL>"}` and returns `{"code": "<short code>", "shortUrl": "<base>/<code>"}`. Invalid or non-http(s) URLs are rejected with 400.
- `GET /{code}` redirects (302) to the original URL and counts one click. Unknown codes return 404.
- `GET /stats/{code}` returns `{"code": "...", "url": "...", "clicks": <n>}`.
- Click counting must be correct under concurrent requests.
- `GET /health` returns 200 when the service and its database are up.
- Data is stored in PostgreSQL. The service runs with `docker compose up`.
- The service is built with .NET and has unit and integration tests (integration tests use Testcontainers).
