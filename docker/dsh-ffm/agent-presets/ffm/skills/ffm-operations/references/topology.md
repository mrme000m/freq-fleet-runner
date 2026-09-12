# topology.md — producer/consumer links & remote reachability

## Producer → consumer linking

Freqtrade shares a producer's `analyzed_df` + whitelist over the message
WebSocket. `ft_instance_link_producer` builds the
`external_message_consumer.producers[]` entry for a consumer:

- Requires the producer's `ws_token` secret
  (`ft_secret_set <producer> ws_token …`) — it refuses without one.
- `host` + `port` are derived from the producer's `base_url`; `secure: true`
  uses `wss`; `link_name` renames the link (defaults to the producer name).
- `initial_candle_limit` (≤1500), `message_size_limit` (1–20 MB),
  `remove_entry_exit_signals` carry through as-is.
- `write: false` (default) returns the entry (ws_token redacted) + a
  JSON snippet with a `<FTMGR_<NAME>_WS_TOKEN>` placeholder and merge
  instructions.
- `write: true` reads the consumer's `config.json` (local via fs, remote via
  ssh), upserts the producer into `external_message_consumer.producers`
  (deduping by `name`), and writes it back. Restart the consumer afterward
  (`ft_stop` + `ft_start`, or `docker restart` for container deploys).

Verify with `ft_show_config` → `external_message_consumer` block.

## Remote API reachability

The agent reaches an instance over its `base_url`. For `host=ssh` instances the
API must be bound to a reachable interface:

- `api_server.listen_ip_address` must be `0.0.0.0` (not `127.0.0.1`) on the
  remote host — `ft_config_generate`/`ft_deploy_ssh` set this automatically
  for `host=ssh`.
- Otherwise reach it through an SSH tunnel (the deploy docs show the
  `-F`/ProxyCommand pattern in `docker/dsh-ffm`).
- The dashboard's `GET /freqtrade/api/fleet?ping=1` pings every instance's
  `/api/v1/ping` (public, no auth) in parallel and reports up/latency.

## Multi-instance layout

- Producers stay on `dry_run`; consumers can be live but only trade what the
  producer's signals allow. Keep `ws_token` unique per producer and in the
  credentials service — never inline in `config.json`.
- A consumer's `external_message_consumer` needs `enabled: true` and at least
  one producer; `ft_instance_link_producer` sets `enabled` unless the config
  already has `enabled: false`.