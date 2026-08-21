# People and access

Everything on this page is **Settings → Instance**, and that tab is only there if
you administer the instance.

## Who administers it

Whoever set it up. That is deliberately not the same as owning something in it:
every account gets a personal organization on signup with itself as owner, so
"owner" is true of everyone and grants nothing over anyone else.

An administrator can change the domain, decide who may sign in, and add or remove
accounts. Everyone else just uses the thing.

To hand it over or share it, **Make admin** on someone in the list. The last
administrator cannot be removed — an instance nobody can administer is
recoverable only with a database client.

## Adding somebody

**Settings → Instance → People**, type their email, **Add person**.

You get a password to pass on, shown once. Nothing stores it — the row holds a
hash — so copy it before you close the panel. They can change it once they are
in.

!!! question "Why a password and not an invitation email?"
    Because most instances of this are one person and a VPS with no mail server,
    and refusing to add anyone until SMTP exists would be correct and useless.

    Configure SMTP under **Ways to sign in** and magic links start working, at
    which point people can get themselves in without a password at all.

!!! tip "Open registration is the other way"
    **Let other people create accounts** lets anyone who can reach the address
    sign themselves up. It starts off, and off is the right answer for anything
    on the public internet — an address that is reachable is an address that gets
    found.

## Removing somebody

**remove**, next to their name.

Refused while they still own projects, and the reason is worth knowing: deleting
an account takes its personal organization with it, and that cascade takes its
servers and projects too. The containers on those machines would carry on running
with nothing left pointing at them — a mess that stays invisible until somebody
wonders why a server is busy.

So deal with the work first: delete the projects, or have them hand them over.

## Ways to sign in

**Settings → Instance → Ways to sign in.** Changes take effect within a few
seconds and nothing restarts.

| Method | Needs |
| ------ | ----- |
| Email and password | Nothing |
| Magic link | An SMTP server, configured on the same screen |
| Google | A domain, and a client id and secret from Google |
| Microsoft | A domain, and a client id and secret from Azure |

Google and Microsoft are unavailable until the instance has a domain, because
neither will redirect to a bare IP address. The screen says so rather than
letting you configure a button that cannot work.

The redirect URI to paste into either console is shown on that screen — it is
derived from your domain, so it is right by construction.

!!! warning "Do not turn off the only way in"
    Turning every method off would lock everybody out, including you. The screen
    says so — *"No sign-in method is usable — nobody could get in"* — and does
    not stop you, so read it.

    The same warning appears when a method is on but cannot work: Google without
    its secret, magic links without SMTP. Those are the same mistake, found
    before somebody clicks a button that goes nowhere.

## The address

**Settings → Instance → Address** is the domain people use. Setting it is what
makes HTTPS possible: the certificate is obtained the first time somebody visits
by name, and renews itself.

Type the name on its own — `moonphase.example.com`. `https://` is filled in for
you, because that is what a certificate gives you. A scheme you type yourself is
kept, and an IP address gets `http://`, since no certificate authority will issue
for one.

[Pointing a domain at it](dns.md) has the exact DNS record, per provider.

Leave it blank and the instance answers on its IP address. That works, and costs
you HTTPS — which costs you push notifications and the ability to install the app
on a phone, since browsers require a secure context for both.

## Sharing, which is a different thing

Adding a person gives them an account. It gives them nothing of yours.

To let someone use a server or watch a project, use **Share** on that server or
project — see [sharing](sharing.md). Those are individual grants by email, and
they are described in full in the [security model](../concepts/security.md).
