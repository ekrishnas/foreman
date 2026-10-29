# Click counts go stale after viewing stats

After I look at the statistics for a short link, later clicks on that link never show up when I look again. The number I saw the first time stays frozen.

## Steps to reproduce
1. Create a short link.
2. Open the link once, then open its stats. It shows 1 click. Correct.
3. Open the link again and re-open the stats.

## Expected
The stats show 2 clicks.

## Actual
The stats still show 1 click.

## Logs
```
GET /stats/abc123 200 clicks=1
GET /abc123 302
GET /stats/abc123 200 clicks=1
```
