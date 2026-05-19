# Kubernetes Guestbook with Prometheus and Grafana Monitoring

This stack extends the Pulumi Kubernetes Guestbook example by adding a full Prometheus and Grafana monitoring layer. It uses the Prometheus Operator pattern with `ServiceMonitor` CRs for declarative scrape configuration, exporter sidecars on every guestbook pod (the base images do not expose `/metrics`), and a Grafana dashboard provisioned as a `ConfigMap` so it lives in version control.

Upstream reference: https://github.com/pulumi/examples/blob/master/kubernetes-ts-guestbook

## What gets deployed

| Component | Namespace | Purpose |
|---|---|---|
| `redis-leader` Deployment and Service | `default` | Backend write store. Includes a `redis-exporter` sidecar on port 9121. |
| `redis-replica` Deployment and Service | `default` | Backend read replicas. Includes a `redis-exporter` sidecar on port 9121. |
| `frontend` Deployment and Service | `default` | PHP and Apache web app. Includes an `apache-exporter` sidecar on port 9117. |
| `kube-prometheus-stack` (Helm) | `monitoring` | Prometheus, Grafana, Alertmanager, kube-state-metrics, node-exporter. |
| 3 `ServiceMonitor` CRs (`frontend`, `redis-leader`, `redis-replica`) | `monitoring` | Tells the Prometheus Operator what to scrape. |
| `guestbook-dashboard` ConfigMap | `monitoring` | Auto-loaded into Grafana via the dashboard sidecar. |

## Prerequisites

* macOS or Linux
* Docker Desktop with Kubernetes enabled, or any Kubernetes cluster
* Pulumi CLI (`brew install pulumi`)
* `kubectl` configured against the target cluster (`kubectl config current-context`)
* Python 3.9 or newer

## Deploy

```bash
git clone <this repo>
cd k8s-guestbook

# Create a virtualenv and install dependencies.
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Pick a Pulumi backend.
pulumi login                # cloud backend
# OR: pulumi login --local  # state on local disk

# Initialise the stack.
pulumi stack init dev

# Set the Grafana admin password as a Pulumi secret (encrypted in state).
pulumi config set --secret grafanaAdminPassword admin123
# Optional override of the username:
# pulumi config set grafanaAdminUser admin

# Bring up the stack.
pulumi up --yes
```

First-time deploy takes roughly 2 minutes. When it finishes Pulumi prints the access details:

```
Outputs:
    frontend_ip            : "10.x.x.x"
    grafana_admin_password : [secret]
    grafana_admin_user     : "admin"
    grafana_dashboard      : "Guestbook Application (auto-loaded via sidecar)"
    grafana_url            : "http://localhost:31000"
```

## Access Grafana

| Setting | Value |
|---|---|
| URL | `http://localhost:31000` |
| Username | `admin` (whatever you set as `grafanaAdminUser`) |
| Password | whatever you set as `grafanaAdminPassword` |

Reveal the password from Pulumi state at any time:

```bash
pulumi stack output grafana_admin_password --show-secrets
```

Grafana is exposed as a `NodePort` Service on port 31000. On Docker Desktop, `localhost:31000` routes directly to the cluster node.

### View the dashboard

After logging in, navigate to **Dashboards, Browse, Guestbook Application**. It contains eight panels:

1. **Frontend Request Rate (req/s per pod)** from `rate(apache_accesses_total[2m])`.
2. **Error Rate: Container Restarts** from `rate(kube_pod_container_status_restarts_total[5m])`.
3. **Redis Commands Processed (ops/s)** from `rate(redis_commands_processed_total[2m])`.
4. **Redis Connected Clients** from `redis_connected_clients`.
5. **Redis Memory Used (MiB)** from `redis_memory_used_bytes`.
6. **Pod CPU Usage (cores)** from cAdvisor (`container_cpu_usage_seconds_total`).
7. **Pod Memory (working set, MiB)** from cAdvisor (`container_memory_working_set_bytes`).
8. **Targets UP** from `up{job=~"frontend|redis-leader|redis-replica"}`.

## Verify Prometheus is scraping Guestbook metrics

```bash
kubectl port-forward -n monitoring svc/kps-kube-prometheus-stack-prometheus 9090:9090
```

Open `http://localhost:9090/targets`. The three pools `frontend`, `redis-leader`, `redis-replica` should be `UP`. Sample queries to confirm data flow: `rate(apache_accesses_total[2m])`, `redis_up`, `rate(redis_commands_processed_total[2m])`, `container_memory_working_set_bytes{namespace="default",pod=~"frontend-.*|redis-.*"}`.

## Configuration

Pulumi stack config (set with `pulumi config set [--secret] <key> <value>`):

| Key | Type | Default | Purpose |
|---|---|---|---|
| `grafanaAdminPassword` | secret (required) | none | Grafana login password. Encrypted in `Pulumi.<stack>.yaml`. |
| `grafanaAdminUser` | plain (optional) | `admin` | Grafana login username. |
| `useLoadBalancer` | bool (optional) | `false` | Expose the frontend Service as LoadBalancer instead of ClusterIP. |

Constants in `__main__.py` (edit the source to change):

| Constant | Default | Purpose |
|---|---|---|
| `GRAFANA_NODE_PORT` | `31000` | NodePort for the Grafana Service. |

## Architecture

```text
┌─────────┐
│  User   │
└────┬────┘
     │ Views Dashboards
     ▼
┌──────────────┐         ┌─────────────────────────┐
│              │ Queries │                         │
│   Grafana    │ ◄────── │       Prometheus        │
│ (Visuals &   │         │ (Central Data Storage)  │
│  Dashboards) │         │                         │
└──────────────┘         └──────────┬──────────────┘
                                    │
                                    │ Pulls Metrics (Scrapes)
                                    ▼
════════════════════════════════════════════════════════════
  KUBERNETES CLUSTER

     [ Application Layer ]
     ┌────────────────────┐      ┌────────────────────┐
     │   Frontend Apps    │      │  Redis Databases   │
     │  (Web interface)   │      │ (Data & Caching)   │
     └────────────────────┘      └────────────────────┘

     [ Infrastructure Layer ]
     ┌────────────────────────────────────────────────┐
     │   Kubernetes Health (Nodes, Pods, Memory)      │
     └────────────────────────────────────────────────┘
════════════════════════════════════════════════════════════
```

### End to end metric flow (example: `apache_accesses_total`)

1. Apache in the `php-redis` container serves `/server-status?auto`. The base image already has mod_status enabled.
2. The `apache-exporter` sidecar in the same pod scrapes that URL, converts the response to Prometheus text format, and exposes it on `:9117/metrics`.
3. The `frontend` Service has a named port `metrics: 9117`.
4. The `frontend` ServiceMonitor selects services labelled `app: frontend` and tells Prometheus to scrape their `metrics` port.
5. The Prometheus Operator (deployed by `kube-prometheus-stack`) watches all `ServiceMonitor` objects, generates the corresponding scrape config, writes it to Prometheus's mounted Secret, and triggers a reload.
6. Prometheus uses the Kubernetes `Endpoints` API to discover the three frontend pod IPs behind the Service and scrapes each pod's `:9117/metrics` every 15 seconds.
7. Grafana queries Prometheus via the in cluster URL `http://kps-kube-prometheus-stack-prometheus.monitoring:9090`, using the provisioned datasource UID `prometheus`.

## Tear down

```bash
pulumi destroy --yes
pulumi stack rm dev --yes
```

Manual cleanup of a stuck Helm release (rare, only if `pulumi destroy` fails partway):

```bash
kubectl delete secret -n monitoring -l owner=helm
kubectl delete ns monitoring
```

## Files

| File | Purpose |
|---|---|
| `__main__.py` | All Pulumi resources. |
| `Pulumi.yaml` | Project metadata. |
| `requirements.txt` | Python dependencies (`pulumi`, `pulumi-kubernetes`). |
| `README.md` | This file. |

## Implementation notes

* **Docker Desktop quirk.** `node-exporter`'s rootfs mount is disabled (`hostRootFsMount.enabled: false`) because Docker Desktop's `/` is not a shared mount, which otherwise crash-loops the DaemonSet pod. The remaining node-exporter metrics (CPU, memory, network) still work.
* **No `/metrics` on base images.** The raw `redis` and `pulumi/guestbook-php-redis` images do not expose Prometheus metrics. Sidecars (`oliver006/redis_exporter`, `lusotycoon/apache-exporter`) translate native status output into Prometheus format.
* **ServiceMonitor versus annotations.** `kube-prometheus-stack` uses the Prometheus Operator. The operator watches `ServiceMonitor` CRDs and generates scrape config; pod or service `prometheus.io/scrape` annotations are ignored unless an `additionalScrapeConfigs` job that honors them is added.
* **Apache error rate.** `apache-exporter` does not expose per HTTP status code counters because mod_status `?auto` does not return them. The dashboard's "Error Rate" panel approximates errors via container restart rate. A production setup would either ship native `/metrics` from the app (OpenTelemetry or the Prometheus PHP client) or use blackbox-exporter probes for per status code observation.

---

## Production hardening roadmap

This stack is the minimum viable deploy. Below is what I would add before shipping to real users, condensed to one row per concern.

| Area | Today | Production target |
|---|---|---|
| Prometheus storage | `emptyDir`; lost on restart. | PVC on fast SSD, 15 to 30 day retention. Long term: Thanos or Mimir to S3 with downsampling. |
| Grafana storage | SQLite in `emptyDir`. | Managed Postgres or MySQL (RDS, Cloud SQL); enables HA and survives pod restarts. |
| Alertmanager storage | `emptyDir`. | PVC plus 3 replica gossip cluster so silences and history survive failover. |
| Pulumi state | Local or personal account. | Pulumi Cloud or S3/GCS backend with state locking, per env stacks, Pulumi ESC for secrets. |
| High availability | Single replicas everywhere. | Prometheus replicas: 2, Alertmanager 3 replica HA, Grafana 2+ replicas with shared DB, PDBs, topology spread across AZs. |
| Resource sizing | Defaults from chart. | Right size `requests` (Prometheus often needs 4+ GiB). Avoid `limits` on Prometheus; OOM during compaction. |
| Secrets | Pulumi config `--secret` (encrypted in state). | External Secrets Operator backed by AWS Secrets Manager, Vault, or SOPS in git. Rotate on schedule; remove the bootstrap value from Pulumi config. |
| AuthN | Local Grafana admin. | OIDC or SAML (Okta, Google, Azure AD). Local admin disabled; emergency account in vault. |
| AuthZ | None. | Grafana RBAC and folder permissions per team. Prometheus stays cluster internal. |
| Network policy | Open. | Deny all default in `monitoring` and `default`; explicit allows for Prom to exporters, Graf to Prom, AM egress. |
| Pod security | Defaults. | `runAsNonRoot`, `readOnlyRootFilesystem`, drop caps, `seccompProfile: RuntimeDefault`; enforced by PSA `restricted` or Kyverno. |
| Supply chain | `:tag` images, unsigned. | Digest pinned images, scanned with Trivy or Snyk, signed with cosign, SBOMs via Syft. |
| Inter component TLS | Plain HTTP. | mTLS via cert-manager and `tlsConfig` on ServiceMonitors. |
| Exposure | NodePort. | Ingress (nginx or Traefik), TLS via cert-manager + Let's Encrypt, OAuth2 Proxy or WAF in front. Prom and AM internal only. |
| Logging | None in this stack. | Loki and Promtail (or Vector or Fluent Bit), JSON structured logs, S3 backed retention, Loki as a second Grafana datasource. |
| Tracing | None. | OpenTelemetry Collector, Tempo or Jaeger backend, app side OTel SDKs, Prometheus exemplars wired to traces. |
| Alerting | Default routes only. | PagerDuty or Opsgenie routes by severity, SLO multi burn rate alerts, recording rules, `runbook_url` on every alert. |
| App metrics | `apache_accesses_total` only. | Native `/metrics` via Prometheus PHP client (RED method, histograms); blackbox-exporter Probes for synthetic checks. |
| Redis topology | 3 independent leaders. | Sentinel or Cluster mode, AOF persistence, or migrate to ElastiCache or MemoryStore. |
| Autoscaling | Static replicas. | HPA on frontend via prometheus-adapter custom metrics, VPA recommendations on monitoring stack, Cluster Autoscaler or Karpenter. |
| Cardinality | Unbounded. | Drop high cardinality labels via `metric_relabel_configs`. Cardinality is the #1 cause of Prometheus OOMs. |
| CI/CD | Manual `pulumi up`. | GitOps (Argo CD or Flux), policy as code (CrossGuard or OPA), per PR ephemeral stacks, per env config. |
| Backup and DR | None. | Velero cluster snapshots, Thanos for Prometheus durability, RDS snapshots for Grafana DB, documented DR runbook with RPO/RTO, quarterly game days. |
| Multi cluster | Single cluster. | Thanos Receive or Mimir centralised, single global Grafana with per cluster datasource variables, optional Cilium or Istio mesh. |
| Cost controls | None. | Resource tags (`team`, `env`, `cost-center`), Kubecost for visibility, spot nodes for stateless tiers, tiered metric retention. |
| Audit and compliance | None. | K8s API audit logs and Grafana audit logs shipped to a SIEM (Splunk, Datadog, ELK). |

Day one priorities if I were taking this to production: (1) Prometheus on a PVC with proper retention, (2) move secrets from Pulumi config to External Secrets backed by a real vault, (3) Grafana behind Ingress with TLS and OIDC. Everything else is incremental.
