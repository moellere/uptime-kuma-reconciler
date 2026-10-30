# Uptime Kuma Reconciler

A Kubernetes controller that automatically discovers Ingress and Istio VirtualService resources and manages [Uptime Kuma](https://github.com/louislam/uptime-kuma) monitors. It bridges the gap between your Kubernetes infrastructure and Uptime Kuma by providing:

- **Auto-discovery** — Monitors are created automatically for any Ingress, Traefik IngressRoute, Gateway API HTTPRoute, or Istio VirtualService annotated with `uptime-kuma.io/monitor: "true"`
- **Static monitors** — Define monitors for non-Kubernetes hosts (network devices, VMs, databases) via a simple YAML ConfigMap
- **Reconciliation loop** — Continuously syncs state, creating new monitors, updating changed ones, and removing orphans
- **Safe tagging** — All managed monitors are tagged with `managed-by-reconciler` so manually-created monitors are never touched

## Why Not kuma-ingress-watcher?

[kuma-ingress-watcher](https://github.com/SQuent/kuma-ingress-watcher) is a great project that covers Ingress and IngressRoute auto-discovery. This reconciler was built to fill gaps that it doesn't cover:

| Feature | kuma-ingress-watcher | uptime-kuma-reconciler |
|---|:---:|:---:|
| Ingress auto-discovery | ✅ | ✅ |
| Traefik IngressRoute | ✅ | ✅ |
| **Gateway API HTTPRoute** | ❌ | ✅ |
| **Istio VirtualService** | ❌ | ✅ |
| **Ping monitors** (VMs, network devices) | ❌ | ✅ |
| **Port monitors** (SSH, MQTT, databases) | ❌ | ✅ |
| **Monitor grouping** | ❌ | ✅ |
| Static monitors via YAML | ✅ (HTTP/TCP only) | ✅ (HTTP, ping, port) |
| Helm chart | ✅ | ✅ |

If you only need HTTP monitoring for Kubernetes Ingresses, kuma-ingress-watcher may be all you need. If you also need to monitor non-HTTP infrastructure (routers, hypervisors, bare-metal hosts) or use Gateway API, this project has you covered.

## How It Works

The reconciler runs as a single-replica Deployment inside your cluster. Every reconciliation cycle (default: 5 minutes) it:

1. Connects to Uptime Kuma via the [uptime-kuma-api](https://github.com/lucasheld/uptime-kuma-api) Python library
2. Lists all Ingress, IngressRoute (Traefik), HTTPRoute (Gateway API), and VirtualService (Istio) resources across all namespaces
3. For each resource with `uptime-kuma.io/monitor: "true"`, creates or updates an HTTP monitor
4. Loads static monitor definitions from a mounted ConfigMap
5. Deletes any managed monitors whose source resource no longer exists

## Installation

### Option 1: Helm Chart

```bash
helm install uptime-kuma-reconciler ./charts/uptime-kuma-reconciler \
  --namespace monitoring \
  --set kumaUrl=http://uptime-kuma:3001 \
  --set credentials.existingSecret=my-kuma-secret
```

The existing secret must contain `username` and `password` keys:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: my-kuma-secret
type: Opaque
stringData:
  username: your-kuma-username
  password: your-kuma-password
```

### Option 2: Docker Image

Build and push the container image:

```bash
docker build -t your-registry/uptime-kuma-reconciler:latest .
docker push your-registry/uptime-kuma-reconciler:latest
```

Then set `image.repository`, `image.tag`, and `image.useCustomImage: true` in the Helm values.

### Option 3: Plain Manifests

Apply the raw Kubernetes manifests directly — see the [charts/uptime-kuma-reconciler/templates/](charts/uptime-kuma-reconciler/templates/) directory for reference.

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `KUMA_URL` | *(required)* | Uptime Kuma URL (e.g., `http://uptime-kuma:3001`) |
| `KUMA_USERNAME` | *(required)* | Uptime Kuma login username |
| `KUMA_PASSWORD` | *(required)* | Uptime Kuma login password |
| `RESYNC_INTERVAL` | `300` | Seconds between full reconciliation cycles |
| `LOG_LEVEL` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### Helm Values

See [charts/uptime-kuma-reconciler/values.yaml](charts/uptime-kuma-reconciler/values.yaml) for all available options.

Key values:

```yaml
kumaUrl: "http://uptime-kuma:3001"
resyncInterval: "300"
logLevel: "INFO"

credentials:
  existingSecret: "my-kuma-secret"  # recommended
  # OR inline (not recommended for production):
  # username: "admin"
  # password: "changeme"

staticMonitors:
  enabled: true
  monitors:
    - name: My Router
      type: ping
      hostname: 192.168.1.1
      group: Network
      interval: 60
```

## Annotations

Add these annotations to your Ingress, IngressRoute, HTTPRoute, or VirtualService resources:

| Annotation | Required | Default | Description |
|---|---|---|---|
| `uptime-kuma.io/monitor` | Yes | — | Set to `"true"` to enable auto-discovery |
| `uptime-kuma.io/monitor-type` | No | `http` | Monitor type: `http`, `keyword`, `ping`, `port` |
| `uptime-kuma.io/monitor-interval` | No | `60` | Check interval in seconds |
| `uptime-kuma.io/monitor-group` | No | — | Group name (created automatically if it doesn't exist) |

### Example Ingress

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-app
  annotations:
    uptime-kuma.io/monitor: "true"
    uptime-kuma.io/monitor-interval: "30"
    uptime-kuma.io/monitor-group: "Web Services"
spec:
  tls:
    - hosts:
        - app.example.com
  rules:
    - host: app.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: my-app
                port:
                  number: 80
```

### Example Istio VirtualService

```yaml
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: my-app
  annotations:
    uptime-kuma.io/monitor: "true"
    uptime-kuma.io/monitor-interval: "30"
    uptime-kuma.io/monitor-group: "Mesh Services"
spec:
  hosts:
    - app.example.com
  gateways:
    - my-gateway
  http:
    - route:
        - destination:
            host: my-app
            port:
              number: 80
```

## Static Monitors

For hosts outside Kubernetes (network devices, VMs, bare-metal servers), define static monitors in a YAML file:

```yaml
monitors:
  - name: Core Router
    type: ping
    hostname: 192.168.1.1
    group: Network
    interval: 60

  - name: Web Dashboard
    type: http
    url: https://dashboard.example.com
    group: Web Services
    interval: 60

  - name: SSH Bastion
    type: port
    hostname: 192.168.1.50
    port: 22
    group: Infrastructure
    interval: 60
```

When using the Helm chart, set `staticMonitors.enabled: true` and list your monitors under `staticMonitors.monitors`.

See [examples/](examples/) for more complete examples.

## Supported Resource Types

| Resource | API Group | Notes |
|---|---|---|
| Ingress | `networking.k8s.io/v1` | Standard Kubernetes Ingress |
| IngressRoute | `traefik.io/v1alpha1` | Traefik CRD |
| HTTPRoute | `gateway.networking.k8s.io/v1` | Gateway API |
| VirtualService | `networking.istio.io/v1` | Istio; URL derived from the first non-wildcard `spec.hosts` entry, always `https://` |

## Monitor Naming

- **Auto-discovered monitors** are named `namespace/Kind/name` (e.g., `default/Ingress/my-app`)
- **Static monitors** are named `static/Name` (e.g., `static/Core Router`)

This naming scheme ensures uniqueness and makes it easy to trace monitors back to their source.

## RBAC

The reconciler needs read-only access to Ingress-like resources across all namespaces. The Helm chart creates the required `ClusterRole` and `ClusterRoleBinding` automatically:

```yaml
rules:
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["traefik.io"]
    resources: ["ingressroutes"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["gateway.networking.k8s.io"]
    resources: ["httproutes"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["networking.istio.io"]
    resources: ["virtualservices"]
    verbs: ["get", "list", "watch"]
```

## License

[MIT](LICENSE)
