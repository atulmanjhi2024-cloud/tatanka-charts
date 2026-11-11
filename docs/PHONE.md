# Phone and PC app

The app on an iPhone, Android phone or PC over Tailscale, and the login that keeps it yours.

## Install it as an app: iPhone, Android, Windows, Mac (Tailscale)

The app installs from the browser as a real app: its own icon, its own window with no browser bars, and
notifications. No App Store, no Mac, no developer account. It is the same app on every device; updates arrive on
the next open.

### 1. Once: Tailscale and the address

Tailscale is a free private network between your own devices: the phone reaches the PC from anywhere (home Wi-Fi or
mobile data), and nothing is open to the internet. The installed app needs an https:// address (service workers and
notifications only run on HTTPS); Tailscale gives it one.

1. On the PC that runs Tatanka Charts: install Tailscale (tailscale.com/download) and sign in.
2. On each phone or other PC: install the Tailscale app (App Store, Google Play, or tailscale.com/download) and sign
   in with the **same account**. Leave it connected.
3. On the PC, once (Command Prompt, PowerShell or Terminal):

   ```
   tailscale serve --bg 8800
   ```

   It prints the app's address, `https://<pc-name>.<tailnet>.ts.net`. If it says HTTPS isn't enabled for your
   tailnet, open the link it prints (Tailscale's admin console > DNS: **MagicDNS** and **HTTPS Certificates** on), then
   run it again. `tailscale serve status` prints the address again later. The container listens on 127.0.0.1 only;
   this publishes it to your own devices, nothing else. Do not use `tailscale funnel` (that is the public internet).
4. Set a password on the PC first (the setup guide, or Connections > **Login**). Until there is one, only the PC
   itself can use the app: a phone gets "Set a password on the PC first".

### 2. Install the app

**iPhone** (iOS 16.4 or later, Tailscale on and connected):

1. Open **Safari**, go to `https://<pc-name>.<tailnet>.ts.net`, and sign in with the password.
2. Tap Share (the square with the arrow), then **Add to Home Screen**, then **Add**. (Safari is the way to use: other
   iPhone browsers have Add to Home Screen in their Share menu too, but the app and its notifications are Safari's.)
3. Open **Tatanka** from the Home Screen icon (not from Safari).
4. Tap the dot at the top right (Connections), scroll to **App notifications**, tap **Turn on for this device**, then
   **Allow**. **Test** sends one.

**Android** (Tailscale on and connected):

1. Open **Chrome**, go to `https://<pc-name>.<tailnet>.ts.net`, and sign in with the password.
2. Chrome's menu (**⋮**, top right) > **Install app** (on some phones: **Add to Home screen**, then **Install**).
   Edge: its menu (**⋯**, bottom) > **Add to phone**. Samsung Internet: menu > **Add page to** > **Home screen**.
3. Open **Tatanka** from its icon (it is in the app drawer too).
4. For notifications, the same as on an iPhone: the dot at the top right > **App notifications > Turn on for this
   device**, then **Allow**.

**Windows or Mac** (Edge or Chrome):

1. Open `https://<pc-name>.<tailnet>.ts.net` (another PC: Tailscale on and connected), or `http://127.0.0.1:8800` on
   the PC that runs it.
2. Edge: **⋯ > Apps > Install this site as an app** (on some versions **⋯ > Apps > Install Tatanka**). Chrome: the
   install icon at the right of the address bar (or **⋮ > Cast, save and share > Install page as app**).
3. It opens in its own window. Windows: it goes on the Start menu; right-click its taskbar icon > **Pin to taskbar**.
   Mac: it goes in Applications (Chrome: Chrome Apps); right-click its Dock icon > **Options > Keep in Dock**.
4. For notifications: in the app, the dot at the top right > **App notifications > Turn on for this device**.
   Notifications need the https:// address; Edge or Chrome must be allowed to run in the background (the default).

To remove it: like any app (iPhone: hold the icon > Remove App; Android: hold the icon > Uninstall; Windows: Start >
right-click > Uninstall; Edge: **⋯ > Apps > Manage apps**).

### Stuck on "Loading the chart…" (red dot top right)

The app opens from the copy it keeps on the device, even when it can't reach the PC; after a few seconds the
loading screen says which it is. If it says it can't reach the server:

1. In Safari, open `https://<pc-name>.<tailnet>.ts.net/api/health`. "Server cannot be found": the phone can't reach
   the PC over Tailscale.
2. In the Tailscale app on the phone, switch it **off and on** (the usual fix: its name lookup stalls), then reload.
3. Still not: Tailscale > Settings > **Use Tailscale DNS settings** on; iPhone Settings > iCloud > **Private Relay**
   off; check the PC shows online in Tailscale and `tailscale serve status` on the PC prints the address.
4. Connected but the chart doesn't come: **Start fresh** on the loading screen drops the saved copy and loads again.

A native App Store app would be this same app inside a wrapper (Capacitor), and needs a Mac with Xcode and Apple's
$99/year developer account; it would add nothing the installed app lacks. A Windows wrapper (Electron) the same.

## Login

**Connections > Login**: set a password (8+ characters) and the app asks for it on every phone and PC once; each
stays signed in (180 days, renewed as you use it, the iPhone app too). The Login card lists the signed-in devices:
right-click one (long-press) to sign it out, **Sign out everywhere else** all but this one, **Change password** signs every other device out.
Ten wrong passwords in ten minutes and it waits ten minutes. Without a password a fresh install lets only the PC
itself in (README, Secure by default), and an older one is open to whoever reaches it, which over Tailscale is only
your own devices; another site's page open on the PC can't use it either (the app
answers only to its own addresses: the PC's, and its `tailscale serve` name, which it takes as this PC's the first time
a browser opens the app by `https://<pc-name>.<tailnet>.ts.net`, and keeps; another name you open it by, the PC's or
your proxy's, or the ts.net name over plain http, goes in `CHART_ALLOWED_HOSTS` in `.env`). A proxy of yours passes on
the address the browser asked for, port included, or the app refuses every change from it ("not from this app"):
nginx `proxy_set_header Host $http_host;` (`$host` drops the port; or add
`proxy_set_header X-Forwarded-Port $server_port;`) and `proxy_set_header X-Forwarded-Proto $scheme;`. Apache, with
its defaults, sends the app's own address as the Host (`127.0.0.1:8800`) and the browser's in `X-Forwarded-Host`: that
name is checked like the Host, so it goes in `CHART_ALLOWED_HOSTS` too. So does the ts.net name of a Tailscale older
than 1.44, which doesn't say its page is https. The password is stored as a scrypt hash.

Forgot it, on the PC (login off, every device signed out):

```
docker compose exec chart python -m chartsrv.auth off
```
