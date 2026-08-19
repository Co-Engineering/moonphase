# Pointing a domain at it

Moonphase needs one DNS record. Once it resolves, HTTPS starts working on its own —
the certificate is obtained the first time someone visits, and renews itself.

## The record

| Field | Value |
| ----- | ----- |
| **Type** | `A` |
| **Name** | the subdomain, e.g. `moonphase` |
| **Value** | your server's public IPv4 address |
| **TTL** | leave it alone, or 300 while you are setting up |

That is the whole thing. No CNAME, no AAAA unless you have IPv6, no proxy.

!!! tip "The Name field is the part people get wrong"
    Almost every provider wants the **subdomain only**, not the full address. For
    `moonphase.example.com` you type `moonphase`, and the provider adds
    `.example.com` for you.

    Typing the whole thing usually produces `moonphase.example.com.example.com`,
    which resolves to nothing and looks exactly like the record has not propagated
    yet.

    A handful of providers do want the full name. The list below says which.

## By provider

=== "Cloudflare"

    **Websites → your domain → DNS → Records → Add record**

    | Field | Value |
    | ----- | ----- |
    | Type | `A` |
    | Name | `moonphase` |
    | IPv4 address | your server's IP |
    | Proxy status | **DNS only** — click the orange cloud to grey it out |
    | TTL | Auto |

    !!! danger "Turn the proxy off"
        The orange cloud proxies traffic through Cloudflare, which terminates TLS
        itself. Caddy then cannot complete the certificate challenge, and the
        WebSocket carrying your terminal goes through an extra hop that will
        idle-timeout on you.

        Grey cloud — **DNS only** — is what you want.

=== "Namecheap"

    **Domain List → Manage → Advanced DNS → Add New Record**

    | Field | Value |
    | ----- | ----- |
    | Type | `A Record` |
    | Host | `moonphase` |
    | Value | your server's IP |
    | TTL | Automatic |

    Namecheap shows `@` for the bare domain. Use the subdomain on its own.

=== "GoDaddy"

    **My Products → DNS → Add New Record**

    | Field | Value |
    | ----- | ----- |
    | Type | `A` |
    | Name | `moonphase` |
    | Value | your server's IP |
    | TTL | 1 Hour |

=== "one.com"

    **Control panel → DNS settings → DNS records**

    | Field | Value |
    | ----- | ----- |
    | Type | `A` |
    | Subdomain / Name | `moonphase` |
    | Content / Value | your server's IP |
    | TTL | 3600 |

=== "Simply.com"

    **Control panel → your domain → DNS**

    | Field | Value |
    | ----- | ----- |
    | Type | `A` |
    | Name | `moonphase` |
    | Data | your server's IP |

=== "Route 53"

    **Hosted zones → your zone → Create record**

    | Field | Value |
    | ----- | ----- |
    | Record name | `moonphase` |
    | Record type | `A` |
    | Value | your server's IP |
    | TTL | 300 |
    | Routing policy | Simple |

    Leave *Alias* off — that is for AWS resources, and this is a plain IP.

=== "Azure DNS"

    **DNS zones → your zone → + Record set**

    | Field | Value |
    | ----- | ----- |
    | Name | `moonphase` |
    | Type | `A` |
    | Alias record set | No |
    | TTL | 300 |
    | IP address | your server's IP |

=== "Squarespace / Google Domains"

    Google Domains became Squarespace Domains. **Domains → your domain → DNS →
    Add record**

    | Field | Value |
    | ----- | ----- |
    | Host | `moonphase` |
    | Type | `A` |
    | Data | your server's IP |
    | TTL | 300 |

=== "Anything else"

    Every provider is asking the same three questions in different words:

    | They may call it | You want |
    | ---------------- | -------- |
    | Type, Record type | `A` |
    | Name, Host, Subdomain, Hostname | `moonphase` |
    | Value, Data, Content, Points to, IP address | your server's IP |

    If a field insists on a fully qualified name, give it
    `moonphase.example.com.` — with the trailing dot, which means "this is the
    whole name, do not append the zone".

## Checking it worked

```console
$ dig +short moonphase.example.com
203.0.113.10
```

If that prints your server's IP, you are done — open the address and Moonphase will
have a certificate within a few seconds of the first visit.

No output means it has not propagated yet. That is usually under a minute and
occasionally an hour, depending on the TTL the record replaced. Nothing needs
restarting while you wait.

```console
$ dig +short moonphase.example.com @1.1.1.1
```

Asking a public resolver directly skips whatever your own machine has cached, which
is the usual reason a record looks missing after it has in fact appeared.

## Then what

Nothing. Put the address into [setup](../getting-started/docker.md), and the first
time the domain is visited the certificate is requested, installed and scheduled for
renewal.

If you set the address before the DNS record existed, that is fine too — it starts
working when the record does.

!!! question "It resolves, but the certificate has not appeared"
    Three things stop it, in order of likelihood:

    1. **Port 80 is not reachable** from the internet. The certificate challenge
       arrives there. Check your firewall and, on a cloud VM, the security group.
    2. **A proxy in front is terminating TLS** — Cloudflare's orange cloud does
       this. Turn it off.
    3. **The address in setup does not match the domain being visited.** Moonphase
       only allows certificates for the name it was told, so that a stranger
       pointing a record at your server cannot mint one against it. Check
       Settings, and that you did not type a trailing slash or a different
       subdomain.
