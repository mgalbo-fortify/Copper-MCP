# Copper CRM MCP server for the ATD team (temporary)

A small MCP server that lets everyone on the team search and update Copper from Claude. It bridges the gap until Copper's official MCP launches.

The server runs entirely on Render's free tier. There's no login: the connector URL contains a long secret code, and that URL works like a password.

## Tools it gives Claude

Read: `search_records` (people, companies, opportunities, leads), `get_record`, `search_opportunities_by_status`, `list_pipelines`, `list_activities`
Write: `create_lead`, `update_record`, `add_note`

New leads and notes are marked "via Claude". Copper shows the API-key owner as the author.

## Setup on Render

1. **Upload the code to a private GitHub repo.** Create a private repo and upload `server.py`, `requirements.txt` and `README.md` (Add file → Upload files).
2. **Get a Copper API key.** In Copper, go to Settings → Integrations → API Keys (this may need admin rights). Note the email of the user who created the key; everyone using the connector acts with that user's permissions.
3. **Make a secret.** Create a random string of at least 32 characters using only letters and numbers. A password manager with symbols turned off works, or run `openssl rand -hex 24` in a terminal. Don't use Render's "Generate" button, because its output can contain characters that break URLs.
4. **Create the Render service.** At render.com, go to New → Web Service and pick the repo. Use these settings:
   - Language: Python 3
   - Build command: `pip install -r requirements.txt`
   - Start command: `python server.py`
   - Instance type: Free
5. **Set environment variables** (in the service's Environment tab):

   | Variable | Value |
   |---|---|
   | `MCP_TRANSPORT` | `http` |
   | `MCP_SECRET` | the secret from step 3 |
   | `COPPER_API_KEY` | from step 2 |
   | `COPPER_USER_EMAIL` | email of the key's owner |

   Don't set `PORT`; Render provides it. The server refuses to start if the secret is missing or too short.
6. **Deploy and check the logs.** Look for "Uvicorn running" in the logs.
7. **Add the connector in Claude.** The connector URL is `https://<your-service>.onrender.com/mcp/<MCP_SECRET>`. Add it as a custom connector; on Team and Enterprise plans, an owner can add it once for everyone. For current steps, see support.claude.com. Test with a read-only search first.

## Keeping it safe

- Treat the full URL like a password. Share it only through a direct message or a password manager, never in a public channel, email thread, or doc.
- To revoke access (for example, if the link leaks or someone leaves the team), change `MCP_SECRET` in Render. The old URL stops working immediately; give the team the new one.
- Anyone with the URL can read and edit Copper with the key owner's permissions. If some people should only read, delete the write tools in a second deployment with its own secret.
- Never put the API key or secret in the code or the repo.

## Keeping it awake (free tier)

Render's free instances sleep after about 15 minutes idle, and waking takes up to a minute, which can make Claude's first request time out. To prevent that, have a free uptime monitor ping the server's `/health` endpoint every 10 minutes:

- **UptimeRobot** (uptimerobot.com) or **cron-job.org**, free plan
- URL: `https://<your-service>.onrender.com/health` (never the `/mcp/<secret>` URL; keep the secret out of third-party tools)
- Interval: 10 minutes

`/health` just returns "ok" and touches no Copper data.

Render's free tier includes 750 instance hours a month, enough to run one service around the clock. If you run other free Render services, they share those hours. To save hours, cron-job.org can ping on a schedule, for example weekdays 8am–7pm only.

## Local option (one person, Claude Desktop)

For a single person, run `python server.py` with just `COPPER_API_KEY` and `COPPER_USER_EMAIL` set, and add it to `claude_desktop_config.json` under `mcpServers` with `command` pointing at your Python and `args` at `server.py`.

## When Copper's official MCP launches

Remove the connector, delete the Render service, and revoke the Copper API key.
