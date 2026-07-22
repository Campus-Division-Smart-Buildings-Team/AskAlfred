# AskAlfred monitoring — optional local stack

> **This is not the deployed monitoring path.** In this environment, AskAlfred
> metrics and logs already ship to **Grafana Cloud** via **Grafana Alloy** running
> on the host: Alloy reads the Prometheus textfiles in `prometheus_textfiles/`
> with its `textfile` collector, drops the `run_id`/`source_path` labels, and
> `remote_write`s to Grafana Cloud (Mimir for metrics, Loki for logs). You do not
> need this stack for normal operation.
>
> This directory is a **self-contained, offline/local-only alternative** —
> Prometheus + Alertmanager + Grafana + node_exporter via docker-compose — for
> when you want metrics, alerts, and dashboards **without** sending anything to
> Grafana Cloud. Typical uses: running the non-production fault-injection matrix
> offline, or local development on an air-gapped host.

## How it fits together (local stack)

The app does **not** expose an HTTP `/metrics` endpoint. Because Streamlit reruns
the script per session, [`core/observability_runtime.py`](../../core/observability_runtime.py)
runs one process-wide publisher that writes Prometheus **textfiles** to
`prometheus_textfiles/` (`SERVICE_METRICS_FILE`, `PROMETHEUS_METRICS_FILE`). This
stack reads them the same way Alloy does — via a textfile collector — but keeps
everything on the host:

```
AskAlfred (host) --writes--> prometheus_textfiles/*.prom
      --textfile collector--> node_exporter :9100/metrics
      --scrape--> Prometheus :9090 --alerts--> Alertmanager :9093
                        \--query--> Grafana :3000
```

## Run it

From this directory:

```bash
docker compose up -d
```

| Service      | URL                     | Notes                                   |
|--------------|-------------------------|-----------------------------------------|
| Grafana      | http://localhost:3000   | admin login; password from env (below)  |
| Prometheus   | http://localhost:9090   | Status → Targets shows `askalfred-textfiles` UP |
| Alertmanager | http://localhost:9093   | Routing only; no receiver is wired yet  |

The **AskAlfred Overview** dashboard is auto-provisioned in the *AskAlfred*
folder. Its data source is a `${datasource}` template variable so the same JSON
imports cleanly into Grafana Cloud; in this local stack it defaults to the
provisioned Prometheus data source.

## Customize before relying on it

1. **Grafana admin password** — set it in `ops/monitoring/.env` (auto-read by
   Compose and already git-ignored via `*.env`), e.g.:

   ```
   GRAFANA_ADMIN_PASSWORD=your-strong-password
   ```

   Or export `GRAFANA_ADMIN_PASSWORD` in the shell before the **first**
   `docker compose up -d`. Grafana only seeds the admin password on first start;
   to change it later use the Grafana UI or `grafana-cli admin reset-admin-password`
   (or `docker compose down -v` to wipe the volume and re-seed).
2. **Alertmanager receivers** — `alertmanager/alertmanager.yml` ships with empty
   (null) receivers, so alerts are grouped but **never delivered**. Add a real
   Slack / email / PagerDuty integration if you want notifications from the local
   stack.

## Files

| Path | Purpose |
|------|---------|
| `docker-compose.yml` | The four services + node_exporter textfile collector |
| `prometheus/prometheus.yml` | Scrape + rule + Alertmanager wiring |
| `prometheus/rules/infra_alerts.yml` | Exporter-down / stale-metrics / parse-error alerts |
| `alertmanager/alertmanager.yml` | Routing + (placeholder) receivers |
| `grafana/provisioning/` | Datasource + dashboard providers |
| `grafana/dashboards/askalfred_overview.json` | Starter dashboard (data-source variable) |

Application **outcome** alert rules are **not** duplicated here — Prometheus
mounts the generated [`ops/askalfred_alerts.yml`](../askalfred_alerts.yml)
directly. Regenerate that file with `python scripts/gen_alert_rules.py` after
changing `core/alerts.py`.

## Using the artifacts with Grafana Cloud instead

The two portable artifacts here also feed the real (Cloud) path:

- **Alert rules** — load the PromQL in [`ops/askalfred_alerts.yml`](../askalfred_alerts.yml)
  into the Grafana Cloud Mimir ruler:

  ```bash
  mimirtool rules load ../askalfred_alerts.yml \
    --address=https://prometheus-prod-55-prod-gb-south-1.grafana.net \
    --id=<tenant-id> --key=$GCLOUD_RW_API_KEY
  ```

- **Dashboard** — import `grafana/dashboards/askalfred_overview.json` into Grafana
  Cloud (Dashboards → New → Import) and pick your Cloud Prometheus data source when
  prompted for `${datasource}`.

Full rollout closure still requires the evidence that
[`tools/validate_rollout_evidence.py`](../../tools/validate_rollout_evidence.py)
gates on (monitoring connected + operator approvals).
