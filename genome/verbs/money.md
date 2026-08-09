# verb: money

- `money.quote(item, price, vendor)` — shows a payment card inline
- `money.pay({amount, vendor, method})` — BLOCKING GATE: pauses for explicit approval, then executes (Stripe/Shopify)
- `money.track(category)` — spend ledger from tool_result events

Rules: any money = blocking, always. No workaround paths. Price watchers (tracker kind=price) assert `price < last_price` before recommending.
