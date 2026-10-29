# Link analytics: what should we build?

Product wants "analytics" for short links, but nobody agrees on what that means or how much to invest. We need a recommendation before committing to a feature.

## Context
The service stores one row per link with a click counter. Raw click events are not kept today.

## Questions
- Which metrics matter most: total clicks, unique visitors, referrers, geography?
- Do we need real-time numbers, or are hourly updates fine?
- What is the expected click volume per day?
