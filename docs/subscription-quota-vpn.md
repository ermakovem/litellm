# VPN subscription quota

This feature uses the native LiteLLM Chat workspace. A profile's assigned share is shown as a personal allowance starting at 100%. The Usage tab includes reset timestamps and lets proxy administrators change the assigned shares.

## Configure the proxy

Set these environment variables on the proxy:

- `LITELLM_VPN_PROXY_SECRET` is a random shared secret for the private HTTP connection between the trusted reverse proxy and LiteLLM.
- `LITELLM_SUBSCRIPTION_QUOTA_DB` is the path to a SQLite file on writable persistent local storage. Run this installation on one host with one writable ledger.
- `LITELLM_SUBSCRIPTION_QUOTA_MODELS` maps the public model name to a source and relative weights. For example, `{"vpn-test":{"source":"Codex QA","input_weight":1,"cached_input_weight":0.1,"output_weight":4}}`.

Register `litellm.proxy.hooks.subscription_quota.subscription_quota_callback` in `litellm_settings.callbacks` and set `general_settings.enable_chat_ui: true`. Provision the profiles as LiteLLM users with IDs of the form `vpn-` followed by 32 lowercase hexadecimal characters. Use `internal_user` for users and `proxy_admin` for operators.

Keep the gateway behind the reverse proxy. On the VPN virtual host, strip client-provided `X-VPN-User-Id` and `X-VPN-Proxy-Secret`, then set the headers from the authenticated VPN mapping and server configuration. Send the original HTTPS scheme with the reverse proxy's trusted forwarding configuration. Redirect the site's entry point and the native `/ui/login` path to `/vpn/session` to issue a LiteLLM session and redirect to Chat. The same identity headers must reach LiteLLM on subsequent API and dashboard requests.

## Provide quota observations

The proxy administrator can send authenticated `PUT /subscription-quota/snapshot` requests with these fields:

```json
{
  "source": "Codex QA",
  "window": "week",
  "used_percent": 30,
  "resets_at": 1795132800,
  "observed_at": 1795096800
}
```

Use actual Unix timestamps for each observation. The fixture model can be configured with a local `mock_response` for a no-charge test. Write a fresh observation at least every three minutes for every configured window. If an upstream window disappears or stops reporting a reset time, the gate rejects requests for that source once the recorded window becomes stale. Preview current windows with `python scripts/subscription_quota_codex.py --source codex` on a machine with a signed-in Codex App Server. To publish from an administrator's machine, add `--proxy-url` and supply `LITELLM_QUOTA_ADMIN_KEY` through the environment.

## Accounting behavior

The ledger takes the first observation as a baseline. A positive change is distributed among the completed, recorded requests since the last positive change, in proportion to each profile's model-weighted usage. All keys and chat requests issued by a profile share the same allowance. Each source and provider window maintains its own cursor and reset. Repeated observations cannot apply a second charge. Usage events received after a settlement are eligible for the next positive observation.

The split is an estimate. A rise with no matching local requests is shown to the administrator as unallocated. If the provider includes outside activity in a period with local requests, that activity can bias the estimate. Paid or production use should use the quota signals and authorization model for the relevant workspace, account and integration.

Use model aliases pinned to one subscription source. The gate checks the last settled allowance before submitting each request. In-flight requests can overlap before the next observation and can take the measured usage above a share. The gate then blocks subsequent requests for that profile until its share grows or the applicable window resets.
