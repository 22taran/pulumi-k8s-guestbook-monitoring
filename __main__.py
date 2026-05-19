"""Pulumi K8s Guestbook with Prometheus + Grafana monitoring."""

import json

import pulumi
from pulumi_kubernetes.apiextensions import CustomResource
from pulumi_kubernetes.apps.v1 import Deployment
from pulumi_kubernetes.core.v1 import ConfigMap, Namespace, Service
from pulumi_kubernetes.helm.v3 import Release, ReleaseArgs, RepositoryOptsArgs

# ---------------------------------------------------------------------------
# Stack configuration
# ---------------------------------------------------------------------------
config = pulumi.Config()
use_load_balancer = config.get_bool("useLoadBalancer")

# Grafana admin user is non-sensitive; default to "admin" unless overridden.
grafana_admin_user = config.get("grafanaAdminUser") or "admin"

# Password is required and must be a Pulumi secret. Set it once with:
#   pulumi config set --secret grafanaAdminPassword <value>
grafana_admin_password = config.require_secret("grafanaAdminPassword")

GRAFANA_NODE_PORT = 31000


# ===========================================================================
# Backend (Redis) deployments and services
# ===========================================================================

redis_leader_labels = {"app": "redis-leader"}

redis_leader_deployment = Deployment(
    "redis-leader",
    spec={
        "selector": {"match_labels": redis_leader_labels},
        "replicas": 3,
        "template": {
            "metadata": {"labels": redis_leader_labels},
            "spec": {
                "containers": [
                    {
                        "name": "redis-leader",
                        "image": "redis",
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "100Mi"},
                        },
                        "ports": [{"container_port": 6379}],
                    },
                    {
                        "name": "redis-exporter",
                        "image": "oliver006/redis_exporter:v1.62.0",
                        "env": [
                            {"name": "REDIS_ADDR", "value": "redis://localhost:6379"},
                        ],
                        "ports": [{"name": "metrics", "container_port": 9121}],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "50Mi"},
                        },
                    },
                ],
            },
        },
    },
)

redis_leader_service = Service(
    "redis-leader",
    metadata={"name": "redis-leader", "labels": redis_leader_labels},
    spec={
        "ports": [
            {"name": "redis", "port": 6379, "target_port": 6379},
            {"name": "metrics", "port": 9121, "target_port": 9121},
        ],
        "selector": redis_leader_labels,
    },
)

redis_replica_labels = {"app": "redis-replica"}

redis_replica_deployment = Deployment(
    "redis-replica",
    spec={
        "selector": {"match_labels": redis_replica_labels},
        "replicas": 3,
        "template": {
            "metadata": {"labels": redis_replica_labels},
            "spec": {
                "containers": [
                    {
                        "name": "redis-replica",
                        "image": "pulumi/guestbook-redis-replica",
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "100Mi"},
                        },
                        "env": [{"name": "GET_HOSTS_FROM", "value": "dns"}],
                        "ports": [{"container_port": 6379}],
                    },
                    {
                        "name": "redis-exporter",
                        "image": "oliver006/redis_exporter:v1.62.0",
                        "env": [
                            {"name": "REDIS_ADDR", "value": "redis://localhost:6379"},
                        ],
                        "ports": [{"name": "metrics", "container_port": 9121}],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "50Mi"},
                        },
                    },
                ],
            },
        },
    },
)

redis_replica_service = Service(
    "redis-replica",
    metadata={"name": "redis-replica", "labels": redis_replica_labels},
    spec={
        "ports": [
            {"name": "redis", "port": 6379, "target_port": 6379},
            {"name": "metrics", "port": 9121, "target_port": 9121},
        ],
        "selector": redis_replica_labels,
    },
)


# ===========================================================================
# Frontend (PHP + Apache) deployment and service
# ===========================================================================

frontend_labels = {"app": "frontend"}

frontend_deployment = Deployment(
    "frontend",
    spec={
        "selector": {"match_labels": frontend_labels},
        "replicas": 3,
        "template": {
            "metadata": {"labels": frontend_labels},
            "spec": {
                "containers": [
                    {
                        "name": "php-redis",
                        "image": "pulumi/guestbook-php-redis",
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "100Mi"},
                        },
                        "env": [{"name": "GET_HOSTS_FROM", "value": "dns"}],
                        "ports": [{"container_port": 80}],
                    },
                    {
                        "name": "apache-exporter",
                        "image": "lusotycoon/apache-exporter:v1.0.6",
                        "args": [
                            "--scrape_uri=http://localhost/server-status?auto",
                        ],
                        "ports": [{"name": "metrics", "container_port": 9117}],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "50Mi"},
                        },
                    },
                ],
            },
        },
    },
)

frontend_service = Service(
    "frontend",
    metadata={"name": "frontend", "labels": frontend_labels},
    spec={
        "type": "LoadBalancer" if use_load_balancer else "ClusterIP",
        "ports": [
            {"name": "http", "port": 80, "target_port": 80},
            {"name": "metrics", "port": 9117, "target_port": 9117},
        ],
        "selector": frontend_labels,
    },
)

if use_load_balancer:
    ingress = frontend_service.status.apply(
        lambda status: status["load_balancer"]["ingress"][0]
    )
    frontend_ip = ingress.apply(
        lambda ing: ing.get("ip", ing.get("hostname", ""))
    )
else:
    frontend_ip = frontend_service.spec.apply(
        lambda spec: spec.get("cluster_ip", "")
    )
pulumi.export("frontend_ip", frontend_ip)


# ===========================================================================
# Monitoring stack: Prometheus, Grafana, Alertmanager via Helm
# ===========================================================================

monitoring_ns = Namespace(
    "monitoring",
    metadata={"name": "monitoring"},
)

kube_prometheus_stack = Release(
    "kube-prometheus-stack",
    args=ReleaseArgs(
        name="kps",
        chart="kube-prometheus-stack",
        namespace=monitoring_ns.metadata["name"],
        repository_opts=RepositoryOptsArgs(
            repo="https://prometheus-community.github.io/helm-charts",
        ),
        values={
            "grafana": {
                "service": {
                    "type": "NodePort",
                    "nodePort": GRAFANA_NODE_PORT,
                },
                "adminUser": grafana_admin_user,
                "adminPassword": grafana_admin_password,
            },
            "prometheus": {
                "prometheusSpec": {
                    "serviceMonitorSelectorNilUsesHelmValues": False,
                    "podMonitorSelectorNilUsesHelmValues": False,
                },
            },
            # docker-desktop: "/" not a shared mount, node-exporter rootfs crash-loops
            "prometheus-node-exporter": {
                "hostRootFsMount": {"enabled": False},
            },
        },
    ),
    opts=pulumi.ResourceOptions(depends_on=[monitoring_ns]),
)


# ===========================================================================
# ServiceMonitors: one per guestbook service
# ===========================================================================

def _service_monitor(name: str, app_label: str) -> CustomResource:
    return CustomResource(
        f"{name}-servicemonitor",
        api_version="monitoring.coreos.com/v1",
        kind="ServiceMonitor",
        metadata={
            "name": name,
            "namespace": "monitoring",
            "labels": {"release": "kps"},
        },
        spec={
            "namespaceSelector": {"matchNames": ["default"]},
            "selector": {"matchLabels": {"app": app_label}},
            "endpoints": [
                {"port": "metrics", "interval": "15s", "path": "/metrics"},
            ],
        },
        opts=pulumi.ResourceOptions(depends_on=[kube_prometheus_stack]),
    )


frontend_servicemonitor = _service_monitor("frontend", "frontend")
redis_leader_servicemonitor = _service_monitor("redis-leader", "redis-leader")
redis_replica_servicemonitor = _service_monitor("redis-replica", "redis-replica")


# ===========================================================================
# Grafana dashboard provisioned via ConfigMap sidecar
# ===========================================================================

def _panel(panel_id, title, grid_pos, expr,
           unit="short", panel_type="timeseries", legend="{{pod}}"):
    return {
        "id": panel_id,
        "title": title,
        "type": panel_type,
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "gridPos": grid_pos,
        "targets": [{
            "expr": expr,
            "legendFormat": legend,
            "refId": "A",
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            # range+instant: Grafana 11+ panels go blank without these
            "range": True,
            "instant": False,
            "editorMode": "code",
        }],
        "fieldConfig": {"defaults": {"unit": unit}},
    }


guestbook_dashboard = {
    "annotations": {"list": []},
    "editable": True,
    "schemaVersion": 39,
    "title": "Guestbook Application",
    "uid": "guestbook",
    "tags": ["guestbook"],
    "time": {"from": "now-30m", "to": "now"},
    "refresh": "15s",
    "templating": {"list": []},
    "panels": [
        _panel(
            1, "Frontend: Request Rate (req/s per pod)",
            {"h": 8, "w": 12, "x": 0, "y": 0},
            'rate(apache_accesses_total{job="frontend"}[2m])',
            unit="reqps",
        ),
        _panel(
            2, "Error Rate: Container Restarts (per 5m)",
            {"h": 8, "w": 12, "x": 12, "y": 0},
            'sum by (pod) (rate(kube_pod_container_status_restarts_total'
            '{namespace="default",pod=~"frontend-.*|redis-leader-.*|redis-replica-.*"}[5m]))',
            unit="short",
        ),
        _panel(
            3, "Redis: Commands Processed (ops/s)",
            {"h": 8, "w": 12, "x": 0, "y": 8},
            'rate(redis_commands_processed_total{job=~"redis-.*"}[2m])',
            unit="ops", legend="{{job}} / {{pod}}",
        ),
        _panel(
            4, "Redis: Connected Clients",
            {"h": 8, "w": 12, "x": 12, "y": 8},
            'redis_connected_clients{job=~"redis-.*"}',
            unit="short", legend="{{job}} / {{pod}}",
        ),
        _panel(
            5, "Redis: Memory Used (MiB)",
            {"h": 8, "w": 12, "x": 0, "y": 16},
            'redis_memory_used_bytes{job=~"redis-.*"} / 1024 / 1024',
            unit="decmbytes", legend="{{job}} / {{pod}}",
        ),
        _panel(
            6, "Pod CPU Usage (cores)",
            {"h": 8, "w": 12, "x": 12, "y": 16},
            'sum by (pod) (rate(container_cpu_usage_seconds_total'
            '{namespace="default",pod=~"frontend-.*|redis-leader-.*|redis-replica-.*"}[2m]))',
            unit="short",
        ),
        _panel(
            7, "Pod Memory (working set, MiB)",
            {"h": 8, "w": 12, "x": 0, "y": 24},
            'sum by (pod) (container_memory_working_set_bytes'
            '{namespace="default",pod=~"frontend-.*|redis-leader-.*|redis-replica-.*"}) '
            '/ 1024 / 1024',
            unit="decmbytes",
        ),
        _panel(
            8, "Targets UP",
            {"h": 8, "w": 12, "x": 12, "y": 24},
            'up{job=~"frontend|redis-leader|redis-replica"}',
            unit="short", legend="{{job}} / {{instance}}",
        ),
    ],
}

guestbook_dashboard_cm = ConfigMap(
    "guestbook-dashboard",
    metadata={
        "name": "guestbook-dashboard",
        "namespace": "monitoring",
        "labels": {"grafana_dashboard": "1"},
    },
    data={"guestbook.json": json.dumps(guestbook_dashboard)},
    opts=pulumi.ResourceOptions(depends_on=[kube_prometheus_stack]),
)


# ===========================================================================
# Stack outputs
# ===========================================================================
pulumi.export("grafana_url", f"http://localhost:{GRAFANA_NODE_PORT}")
pulumi.export("grafana_admin_user", grafana_admin_user)
pulumi.export("grafana_admin_password", grafana_admin_password)
pulumi.export(
    "grafana_dashboard",
    "Guestbook Application (auto-loaded via sidecar)",
)
