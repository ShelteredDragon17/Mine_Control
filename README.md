# loadsettle-curtail

Put your ASIC miners to sleep during expensive time-of-use hours and wake them afterward. Every command is checked against what the miner reports back, so a miner that ignored the command shows up as a failure.

Free and open source (Apache-2.0). It runs on anything that can run Python 3.11+ on the same network as your miners: a Raspberry Pi, an old laptop, a NAS or Docker.

> **Status: early.** So far it has only been tested against the built-in stock-Antminer simulator. Real-hardware reports are wanted; see [Self-test](#self-test).

## Why this exists

Stock Bitmain firmware has no scheduler. The current options are:

* **Paid fleet tools.** Braiins Manager costs $250/MW-month with a $25 minimum. LuxOS Commander charges per MW or a pool fee. Both are built for farms, not one or two machines at home.
* **Custom firmware with a dev fee.** It works, but it's a fee on every hash, forever.
* **Unplugging the network cable.** What a stock miner does when it loses its pool isn't documented. It may keep drawing near-full power while it retries. Sleep mode is the documented way to stop hashing.
* **Home Assistant integrations.** These send the command and assume it worked. On some stock firmware it doesn't.

This tool uses [pyasic](https://github.com/UpstreamData/pyasic) for the miner protocols. It adds a schedule, a read-back check on every command, and fallbacks for firmware quirks.

## What it does

* **Weekly schedule** in local time, so a 4–9 pm window stays at 4–9 pm across daylight-saving changes.
  * Windows can be limited to certain months or dates.
  * A window can run past midnight.
  * A window can use **Low** power mode instead of **Sleep** where the firmware supports it.
* **Confirmed commands.**
  * After sending Sleep or Normal, the agent polls until the miner reports the new mode.
  * On stock Antminers it tries three encodings of the command. It remembers which one works for each miner.
* **Self-correcting.** Every `poll_seconds` it re-checks each miner and fixes any drift, for example after a reboot or someone clicking around in the web UI.
* **Staggered wake-ups.** Miners resume one at a time, `resume_stagger_seconds` apart.
* **Safe when blind.** An unreachable miner is left alone and retried. The agent never acts on a state it can't read.
* **Local status page** with manual override buttons: Sleep 1 h, Run 1 h, Pause automation, Back to schedule.
* **Savings log.** Each run records kWh and dollars avoided during scheduled windows in SQLite.

## Supported miners

The table below covers anything pyasic supports. The stock-Antminer fallbacks are what this project adds.

| Firmware | Sleep / wake path | Verified |
|---|---|---|
| Stock Bitmain (S19, S21 family) | `miner-mode` via the web API, with 2 fallbacks | Simulator only |
| Braiins OS, LuxOS, Vnish, Whatsminer, Avalon… | pyasic's native pause/resume | Not yet |

## Install

```bash
pip install git+https://github.com/<owner>/loadsettle-curtail
```

Or with Docker:

```bash
docker build -t loadsettle-curtail .
```

## Quick start

1. Find your miners:

```bash
curtail scan 192.168.1.0/24
```

2. Run the self-test on one of them. This takes about 5–10 minutes, most of it waiting for the hashrate to recover:

```bash
curtail selftest 192.168.1.50
```

3. Copy `config.example.yaml` to `config.yaml` and set your time zone, miner IPs and on-peak hours.

4. Start the agent:

```bash
curtail run -c config.yaml
```

5. Open http://127.0.0.1:8787 to see the status page.

Other commands:

```bash
curtail sleep 192.168.1.50
```

```bash
curtail wake 192.168.1.50
```

```bash
curtail status
```

## Self-test

`curtail selftest IP` runs these steps:

1. Read the model, firmware and current mode.
2. Send Sleep and confirm the miner reports Sleep.
3. Confirm the hashrate drops to about zero.
4. Send Normal and confirm the miner reports Normal.
5. Confirm the hashrate starts climbing again.

The miner always ends in the mode it started in, even if a step fails. The test also writes `selftest-<ip>-<time>.json`, which records the model, firmware, the encoding that worked and the timings. Please share that file to help build the compatibility list.

**Measure at the wall if you can.** The miner reports hashrate, not true wall power. A cheap plug-in meter or a smart PDU reading during Sleep is the only real proof of what you save. On a stock S21 XP, expect about 20–40 W for the control board and fans, against about 3,600 W while hashing.

## Try it without a miner

```bash
curtail sim --ramp 30
```

That starts a simulated stock S21 XP on http port 8080 and RPC port 4029. Then, in another terminal:

```bash
curtail selftest 127.0.0.1 --http-port 8080 --rpc-port 4029
```

Add `--ignore-writes` to the `sim` command to see how a miner that ignores mode changes gets caught.

## Run it as a service

* **Linux (systemd).** Create a unit with `ExecStart=/path/to/venv/bin/curtail run -c /etc/curtail/config.yaml` and `Restart=always`.
* **Docker.** Run it with `--network host` so it can reach the miners on your LAN:

```bash
docker run -d --restart unless-stopped --network host -v ./config.yaml:/config/config.yaml -v curtail-data:/data loadsettle-curtail
```

Set `data_dir: /data` in the config when using Docker.

## Security

* The status page has **no login**, and it binds to `127.0.0.1` by default. Expose it with `web.host: 0.0.0.0` only on a network you trust.
* Miner web passwords in `config.yaml` are stored in plain text, so keep that file private.

## Development

```bash
pip install -e ".[test]"
```

```bash
pytest
```

The tests run the agent end to end against the simulator. That covers firmware that ignores writes, string-only firmware, the alternate mode key, unreachable miners and staggered wake-ups.

## License

Apache-2.0. Uses pyasic (Apache-2.0).
