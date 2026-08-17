# Usage and limits

What the agents consumed, and how much of your allowance is left.

Everything here is counted from the harness's own transcripts — the same `usage` figures
the provider billed. Nothing is estimated, intercepted, or asked for.

## Two different questions

The same rows answer two questions, and putting the wrong one first makes the screen
useless. Which one leads is decided by the credential you actually connected, not by a
preference you have to find.

=== "On a subscription"

    The limit is a **window**, not money. What matters is how much of the current one has
    gone and when it comes back:

    ```text
    Current session (5h)                    38% used
    ████████░░░░░░░░░░░░░░░░
    Resets 2:29 PM   in 1h 12m   1.2M of 3.1M
    ```

=== "On an API key"

    The limit is money. What matters is the bill:

    ```text
    $2.62 spent in the last 7 days
    ```

## Windows are anchored, not trailing

A subscription's window opens with your first message and resets at a fixed time. It is
**not** "the last five hours".

That distinction is the whole feature. "You used 4.7M tokens" tells you nothing without
knowing when it comes back, and a trailing sum answers a question nobody asked.

When nothing has opened a window, the panel says so rather than reporting a stale one:

> Nothing running. The window opens with your next message.

## Setting your allowance

Anthropic does not publish a token allowance per plan, and Moonphase will not use a
session's own credentials to go and ask on your behalf. So it asks you once.

**Usage → Plan limits.** Put in the token count your plan allows, and the bars become
percentages. Leave a field empty to go back to showing raw tokens.

!!! note "Why there is no bar until you do"
    A bar drawn against a number nobody supplied is decoration pretending to be
    information. Until an allowance is known you get the raw count and an offer to fix
    it — which is the honest version of the same screen.

## Alerts

Set **Warn me when a window reaches** to a percentage and you get a push when you cross
it.

Fired **once per window**, not once per check. A threshold crossed at 60% stays crossed,
and without that rule you would get the same notification every two minutes for the rest
of a five-hour window. A new window rearms it by itself.

Needs a limit set above, and [notifications](from-your-phone.md) turned on.

## Cost, and when it is blank

Cost is tokens × a rate per model, with the cache tiers priced properly:

| Tier              | Rate                |
| ----------------- | ------------------- |
| Input             | base                |
| Output            | base                |
| Cache read        | 0.1 × input         |
| Cache write (5m)  | 1.25 × input        |
| Cache write (1h)  | 2.0 × input         |

Cache reads being a tenth is not a detail. A long agent session is *mostly* cache reads,
so pricing them as fresh input overstates the bill by roughly an order of magnitude.

**A model with no known rate shows no cost**, not `$0.00`. Inventing a rate produces a
confident number that happens to be wrong, which is a worse answer about someone's bill
than no answer. The blank is a button:

> `set rate`

### Model rates

**Usage → Model rates.** Dollars per million tokens, input and output. Cache rates are
derived, because the multipliers are the provider's and are the same for every model —
asking for five numbers where two will do is three more chances to mistype a bill.

A rate applies to every model whose name starts with it, so `claude-sonnet-5` covers every
dated release of it. Rates you set override the built-in table, and the longest matching
prefix wins.

## Privacy

Usage rows are scoped to the user who produced them. Sharing a server or a project never
exposes anyone's spend.
