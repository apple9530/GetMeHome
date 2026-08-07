# Putting the backend on your own domain, with AWS

Written to be followed start to finish without prior knowledge. Every term is
explained the first time it appears.

You will end up with `https://api.vedant.io` answering from a computer that
stays on, and the iOS app talking to it without anyone typing an address.

Budget roughly **$20–40 a month** for the server, plus whatever the domain
costs you already. Check the current price on the AWS page before you commit —
these change, and this file will not.

---

## The shape of it, before any commands

Four things have to be true. Everything below is in service of one of them.

1. **A computer that is always on.** Yours is not — it sleeps, it goes out with
   you. You rent one from Amazon that lives in a data centre.
2. **A name that points at it.** `api.vedant.io` has to mean "that computer".
   That is a DNS record, which you add wherever `vedant.io` is managed.
3. **A padlock.** The iPhone app refuses plain `http://` once it ships, so the
   server needs an HTTPS certificate. Caddy — already in `deploy/` — gets one
   free and renews it forever without being asked.
4. **The map data.** This is the part people trip on. The *program* is small
   and installs itself. The *data* — the street graph, crime, timetables — is
   large, is built from a 400 MB download, and is **not** in the container. You
   build it on your Mac and copy it up.

### One thing to get straight up front

> **Build the data on your Mac. Copy it to the server. Never build it on the
> server.**

Parsing New York's OpenStreetMap extract needs several gigabytes of memory. A
$20 server does not have them, and the build will be killed halfway with a
message that does not explain why. Your Mac has plenty. The server only ever
*reads* the finished files.

---

## Step 1 — Rent the computer

Use **Amazon Lightsail**. It is EC2 with the confusing parts removed: one fixed
monthly price, a simple firewall, and networking that already works. Plain EC2
does the same job if you would rather have the knobs; the only differences that
matter here are that EC2 bills by the hour and makes you assemble the
networking yourself.

1. Sign in at `lightsail.aws.amazon.com`.
2. **Create instance**.
3. Region: pick the one nearest your users. `us-east-1` (Virginia) is next door
   to DC and close to New York.
4. Platform: **Linux/Unix**. Blueprint: **Ubuntu 22.04 LTS**.
   - "Blueprint" means the starting software. Ubuntu is a flavour of Linux.
     "LTS" means it gets security updates for years.
5. Size: **at least 4 GB of memory**. See the sizing note at the bottom — if
   you are serving both cities you may want 8 GB.
6. Name it `getmehome`, then **Create**.

It takes a minute or two to say *Running*.

### Give it a fixed address

A fresh instance's IP address changes if it is ever restarted, which would
silently break your domain.

- In Lightsail, **Networking → Create static IP**, attach it to `getmehome`.
- Write the address down. It looks like `54.163.20.117`.

Static IPs are free while attached to a running instance and charged if you
leave one lying around unattached, so do not create spares.

### Open the right doors

Under your instance → **Networking → IPv4 Firewall**, make sure these exist:

| Port | What it is for |
|---|---|
| 22 | SSH — you typing commands at it |
| 80 | Plain web. Only used to prove you own the domain, and to redirect |
| 443 | HTTPS. The real one |

Delete anything else. **Do not** open 8000. The app must only ever be reachable
through Caddy, which is what guarantees it is only ever reachable over HTTPS.

---

## Step 2 — Point your domain at it

`vedant.io` is managed somewhere — the company you bought it from, or Route 53
if you have already moved it. Go there and add one record:

| Field | Value |
|---|---|
| Type | `A` |
| Name | `api` |
| Value | your static IP, e.g. `54.163.20.117` |
| TTL | 300 |

That makes `api.vedant.io` mean that computer.

**Use a subdomain, not the bare domain.** `api.vedant.io` leaves `vedant.io`
free for a website later. Pointing the apex here would give the whole domain to
this one service, and undoing that is a nuisance.

Wait a few minutes, then from your Mac:

```bash
dig +short api.vedant.io
```

It should print your IP. If it prints nothing, DNS has not caught up — wait
longer. **Do not go on until this works**, because the certificate step in
step 4 depends on it and will fail confusingly.

---

## Step 3 — Get onto the machine

Lightsail has a browser terminal (the orange **Connect using SSH** button),
which is fine for a look around. For real work use your Mac's terminal:

1. Lightsail → **Account → SSH keys → Download** the default key.
2. Then:

```bash
mkdir -p ~/.ssh
mv ~/Downloads/LightsailDefaultKey-*.pem ~/.ssh/lightsail.pem
chmod 600 ~/.ssh/lightsail.pem       # only you may read it; SSH insists
ssh -i ~/.ssh/lightsail.pem ubuntu@api.vedant.io
```

`ubuntu` is the username the image ships with. You should land at a prompt
ending in `$`. That prompt is the server, not your Mac — worth keeping track of,
because the next few commands run in different places.

Make it less tedious by adding this to `~/.ssh/config` on your Mac:

```
Host getmehome
    HostName api.vedant.io
    User ubuntu
    IdentityFile ~/.ssh/lightsail.pem
```

Now `ssh getmehome` is enough.

---

## Step 4 — Install and start the service

**On the server:**

```bash
# Docker runs the app in a sealed box, so nothing on the server needs Python.
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
exit
```

That last line logs you out on purpose — the group change only applies to a new
session. `ssh getmehome` back in, then:

```bash
git clone https://github.com/apple9530/GetMeHome.git
cd GetMeHome/deploy
cp .env.example .env
nano .env
```

Fill in:

```
DOMAIN=api.vedant.io
ACME_EMAIL=you@vedant.io
WMATA_API_KEY=your-key-if-you-have-one
```

`Ctrl-O`, `Enter`, `Ctrl-X` to save and quit nano.

Make the folder the data will land in, then start:

```bash
mkdir -p data
docker compose up -d
```

`-d` means "in the background". Compose builds the image the first time, which
takes a few minutes.

Watch it:

```bash
docker compose logs -f
```

You want a line from Caddy saying it got a certificate, and a line from the app
saying **no city has been built**. That second one is correct — you have not
sent the data yet. `Ctrl-C` stops watching; it does not stop the server.

---

## Step 5 — Send the data up

**On your Mac**, in the repo:

```bash
cd backend
make graph CITY=dc                 # ~20 minutes the first time
make gtfs  CITY=nyc                # if you want New York transit
make graph CITY=nyc                # longer; the extract is the whole state

make deploy-data HOST=ubuntu@api.vedant.io
```

That last command copies every built city up in one pass. Then tell the server
to pick it up:

```bash
ssh getmehome 'cd GetMeHome/deploy && docker compose restart api'
```

Check it:

```bash
curl https://api.vedant.io/health
```

You want `{"status":"ok", ...}`. A padlock in a browser at the same address
means the certificate worked.

If you get `no_graph`, the files did not land. Look on the server:

```bash
ssh getmehome 'ls -la GetMeHome/deploy/data/dc/build'
```

You should see `graph.npz` and friends.

---

## Step 6 — Point the app at it

**On your Mac:**

```bash
cd ios
export GETMEHOME_SERVER_URL=https://api.vedant.io
xcodegen generate
```

Every install now connects to your server on first launch with nobody typing
anything.

### Before you give the app to anyone else

Open `ios/project.yml` and delete these two lines:

```yaml
        NSAppTransportSecurity:
          NSAllowsArbitraryLoads: true
```

They exist so the app can talk to your laptop over plain HTTP while you build.
Leaving them in a released app means every request it makes can be read and
altered by anyone on the same Wi-Fi, and App Review will ask about it. Once
your server is HTTPS you do not need them. Regenerate afterwards.

---

## Keeping it alive

**It restarts itself.** `restart: unless-stopped` in the compose file means
Docker brings the containers back after a crash *and* after the server reboots.
Nothing to configure.

**The certificate renews itself.** Caddy handles it. Nothing to configure.

**The data does not update itself.** Crime moves, and the timetable is loaded
for a single service day, so departure boards drift as schedules change. Once a
week, on your Mac:

```bash
cd backend
make graph-refresh CITY=dc
make deploy-data HOST=ubuntu@api.vedant.io
ssh getmehome 'cd GetMeHome/deploy && docker compose restart api'
```

**Updating the code:**

```bash
ssh getmehome 'cd GetMeHome && git pull && cd deploy && docker compose up -d --build'
```

---

## Sizing, honestly

Everything is held in memory: the street graph, the crime points, the
timetable. **Memory is what you are buying, not CPU.** A server too small does
not run slowly — it is killed during startup, with a log that does not say why.

I cannot tell you the numbers, because they depend on your build. Find them:

```bash
# On your Mac, before you buy anything
cd backend && make data-manifest CITY=dc
make data-manifest CITY=nyc
```

That prints the on-disk size. Memory use is **noticeably higher**, because the
arrays are compressed on disk and expanded on load. Start at 4 GB, then on the
server:

```bash
docker stats
```

If the number under `MEM USAGE` is close to the limit, or the app is restarting
on its own, move up a size. Lightsail can resize an instance from a snapshot.

Two things that make this worse:

- **Both cities loaded at once** costs the memory of both. They load on first
  use and are never released, so a server that survives DC alone may not
  survive someone switching to New York. If you are tight, run
  `GETMEHOME_DEFAULT_CITY` for the one you actually use.
- **`GETMEHOME_PRELOAD_CITIES=all`** loads everything at boot. It makes the
  first request fast and the memory requirement immediate. Do not set it until
  you know both cities fit.

A cheap insurance policy on a small instance:

```bash
# On the server — gives the kernel somewhere to put cold pages
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Swap is disk pretending to be memory. It is much slower, so this turns "killed
during startup" into "slow first request", which is a better failure.

---

## One limit worth knowing

The app runs as **a single process, on purpose**. Live ETA shares are held in
that process's memory, so a second copy would answer "no such share" for half
the requests on a live share. This is fine for one person or a few hundred; it
is a real ceiling if this ever gets popular, and the fix is moving shares to
Redis before adding a second copy. It is not something to pre-emptively worry
about.

---

## If something is wrong

| What you see | Almost always |
|---|---|
| `{"status":"no_graph"}` | The data never arrived. Check `deploy/data/dc/build` on the server |
| Certificate never issued | DNS is not pointing at the server yet, or port 80 is shut. `dig +short api.vedant.io` |
| App says "can't reach the server" | You did not regenerate the Xcode project after setting `GETMEHOME_SERVER_URL` |
| Container restarting in a loop | Out of memory. `docker stats`, then size up or add swap |
| `503` on transit | That city has no timetable. `make gtfs CITY=… && make graph CITY=…`, then re-copy |

Useful in every case:

```bash
ssh getmehome 'cd GetMeHome/deploy && docker compose logs --tail 100 api'
```

---

## Is AWS the right choice?

For this app, honestly: it is a fine choice and not the easiest one. The repo
also has `deploy/fly.toml`, and Fly does the certificate, the persistent disk
and the deployment in about four commands with no firewall or SSH keys to
manage. The trade is less control and a platform you may not want to depend on.

AWS is the better answer if you already have things there, want everything in
one bill, or expect to grow into other AWS services. The steps above are all it
takes.
