"""
Uptime Kuma auto-discovery reconciler.

Watches Ingress, IngressRoute (Traefik), and HTTPRoute (Gateway API) resources
for the annotation `uptime-kuma.io/monitor: "true"` and automatically creates,
updates, and deletes HTTP monitors in Uptime Kuma.

Also reconciles static monitors defined in /config/monitors.yaml for
non-Kubernetes hosts (Proxmox nodes, VMs, network gateways, etc.).
"""

import logging
import os
import signal
import sys
import time
from threading import Event

import yaml
from kubernetes import client, config, watch
from uptime_kuma_api import UptimeKumaApi, MonitorType

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("reconciler")

ANNOTATION_ENABLED = "uptime-kuma.io/monitor"
ANNOTATION_TYPE = "uptime-kuma.io/monitor-type"
ANNOTATION_INTERVAL = "uptime-kuma.io/monitor-interval"
ANNOTATION_GROUP = "uptime-kuma.io/monitor-group"
ANNOTATION_PATH = "uptime-kuma.io/monitor-path"
MANAGED_TAG = "managed-by-reconciler"
STATIC_MONITORS_PATH = "/config/monitors.yaml"

MONITOR_TYPES = {
    "http": MonitorType.HTTP,
    "keyword": MonitorType.KEYWORD,
    "ping": MonitorType.PING,
    "port": MonitorType.PORT,
}

shutdown_event = Event()


def signal_handler(signum, frame):
    log.info("Received signal %s, shutting down...", signum)
    shutdown_event.set()


def connect_kuma(url, username, password):
    api = UptimeKumaApi(url)
    api.login(username, password)
    log.info("Connected to Uptime Kuma at %s", url)
    return api


def get_managed_monitors(api):
    monitors = api.get_monitors()
    managed = {}
    for m in monitors:
        tags = [t.get("name", "") for t in m.get("tags", [])]
        if MANAGED_TAG in tags:
            managed[m["name"]] = m
    return managed


def ensure_tag(api):
    for tag in api.get_tags():
        if tag["name"] == MANAGED_TAG:
            return tag["id"]
    result = api.add_tag(name=MANAGED_TAG, color="#2563eb")
    return result["id"]


def ensure_group(api, group_name):
    if not group_name:
        return None
    monitors = api.get_monitors()
    for m in monitors:
        if m.get("type") == MonitorType.GROUP and m.get("name") == group_name:
            return m["id"]
    result = api.add_monitor(type=MonitorType.GROUP, name=group_name)
    log.info("Created monitor group: %s", group_name)
    return result["monitorID"]


def extract_url_from_resource(resource):
    kind = resource.get("kind", "")
    spec = resource.get("spec", {})

    if kind == "Ingress":
        tls_hosts = set()
        for tls in spec.get("tls") or []:
            for h in tls.get("hosts") or []:
                tls_hosts.add(h)
        for rule in spec.get("rules") or []:
            host = rule.get("host")
            if host:
                scheme = "https" if host in tls_hosts else "http"
                return f"{scheme}://{host}"

    elif kind == "IngressRoute":
        for route in spec.get("routes") or []:
            match_str = route.get("match", "")
            if "Host(" in match_str:
                host = match_str.split("Host(`")[-1].split("`")[0]
                if host:
                    tls = spec.get("tls")
                    scheme = "https" if tls else "http"
                    return f"{scheme}://{host}"

    elif kind == "HTTPRoute":
        for hostname in spec.get("hostnames") or []:
            return f"https://{hostname}"

    return None


def build_monitor_key(resource):
    meta = resource.get("metadata", {})
    kind = resource.get("kind", "")
    ns = meta.get("namespace", "default")
    name = meta.get("name", "unknown")
    return f"{ns}/{kind}/{name}"


def reconcile_resource(api, resource, managed, tag_id):
    annotations = resource.get("metadata", {}).get("annotations") or {}
    enabled = annotations.get(ANNOTATION_ENABLED, "").lower() == "true"
    key = build_monitor_key(resource)

    if not enabled:
        if key in managed:
            log.info("Removing monitor %s (annotation removed)", key)
            try:
                api.delete_monitor(managed[key]["id"])
            except Exception as e:
                log.error("Failed to delete monitor %s: %s", key, e)
        return

    url = extract_url_from_resource(resource)
    if not url:
        log.warning("Cannot extract URL from %s, skipping", key)
        return

    path = annotations.get(ANNOTATION_PATH, "")
    if path:
        url = url.rstrip("/") + "/" + path.lstrip("/")

    monitor_type_str = annotations.get(ANNOTATION_TYPE, "http").lower()
    monitor_type = MONITOR_TYPES.get(monitor_type_str, MonitorType.HTTP)
    interval = int(annotations.get(ANNOTATION_INTERVAL, "60"))
    group_name = annotations.get(ANNOTATION_GROUP, "")
    parent_id = ensure_group(api, group_name) if group_name else None

    if key in managed:
        existing = managed[key]
        needs_update = (
            existing.get("url") != url
            or existing.get("interval") != interval
            or existing.get("type") != monitor_type
        )
        if needs_update:
            log.info("Updating monitor %s -> %s", key, url)
            try:
                kwargs = dict(
                    type=monitor_type, name=key, url=url,
                    interval=interval, retryInterval=60, maxretries=3,
                )
                if parent_id is not None:
                    kwargs["parent"] = parent_id
                api.edit_monitor(existing["id"], **kwargs)
            except Exception as e:
                log.error("Failed to update monitor %s: %s", key, e)
    else:
        log.info("Creating monitor %s -> %s", key, url)
        try:
            kwargs = dict(
                type=monitor_type, name=key, url=url,
                interval=interval, retryInterval=60, maxretries=3,
            )
            if parent_id is not None:
                kwargs["parent"] = parent_id
            result = api.add_monitor(**kwargs)
            monitor_id = result.get("monitorID")
            if monitor_id:
                api.add_monitor_tag(tag_id, monitor_id)
        except Exception as e:
            log.error("Failed to create monitor %s: %s", key, e)


def load_static_monitors():
    """Load static monitor definitions from ConfigMap-mounted YAML."""
    if not os.path.exists(STATIC_MONITORS_PATH):
        log.info("No static monitors file at %s", STATIC_MONITORS_PATH)
        return []
    try:
        with open(STATIC_MONITORS_PATH) as f:
            data = yaml.safe_load(f)
        monitors = data.get("monitors", []) if data else []
        log.info("Loaded %d static monitor definitions", len(monitors))
        return monitors
    except Exception as e:
        log.error("Failed to load static monitors: %s", e)
        return []


def reconcile_static_monitors(api, managed, tag_id):
    """Create/update monitors from static definitions."""
    static_defs = load_static_monitors()
    seen_keys = set()

    for entry in static_defs:
        name = entry.get("name", "")
        if not name:
            continue

        key = f"static/{name}"
        seen_keys.add(key)

        monitor_type_str = entry.get("type", "http").lower()
        monitor_type = MONITOR_TYPES.get(monitor_type_str, MonitorType.HTTP)
        interval = int(entry.get("interval", 60))
        group_name = entry.get("group", "")
        parent_id = ensure_group(api, group_name) if group_name else None

        kwargs = dict(
            type=monitor_type,
            name=key,
            interval=interval,
            retryInterval=60,
            maxretries=3,
        )

        if monitor_type == MonitorType.HTTP:
            url = entry.get("url", "")
            if not url:
                log.warning("Static monitor %s missing url, skipping", name)
                continue
            kwargs["url"] = url
            accepted_codes = entry.get("accepted_statuscodes")
            if accepted_codes:
                kwargs["accepted_statuscodes"] = accepted_codes
        elif monitor_type == MonitorType.PING:
            hostname = entry.get("hostname", "")
            if not hostname:
                log.warning("Static monitor %s missing hostname, skipping", name)
                continue
            kwargs["hostname"] = hostname
        elif monitor_type == MonitorType.PORT:
            hostname = entry.get("hostname", "")
            port = entry.get("port", 80)
            if not hostname:
                log.warning("Static monitor %s missing hostname, skipping", name)
                continue
            kwargs["hostname"] = hostname
            kwargs["port"] = port

        if parent_id is not None:
            kwargs["parent"] = parent_id

        if key in managed:
            existing = managed[key]
            needs_update = False
            if monitor_type == MonitorType.HTTP:
                needs_update = (
                    existing.get("url") != kwargs.get("url")
                    or existing.get("interval") != interval
                    or existing.get("type") != monitor_type
                )
            elif monitor_type == MonitorType.PING:
                needs_update = (
                    existing.get("hostname") != kwargs.get("hostname")
                    or existing.get("interval") != interval
                )
            elif monitor_type == MonitorType.PORT:
                needs_update = (
                    existing.get("hostname") != kwargs.get("hostname")
                    or existing.get("port") != kwargs.get("port")
                    or existing.get("interval") != interval
                )
            if needs_update:
                log.info("Updating static monitor %s", key)
                try:
                    api.edit_monitor(existing["id"], **kwargs)
                except Exception as e:
                    log.error("Failed to update static monitor %s: %s", key, e)
        else:
            log.info("Creating static monitor %s", key)
            try:
                result = api.add_monitor(**kwargs)
                monitor_id = result.get("monitorID")
                if monitor_id:
                    api.add_monitor_tag(tag_id, monitor_id)
            except Exception as e:
                log.error("Failed to create static monitor %s: %s", key, e)

    return seen_keys


def full_reconcile(api, tag_id):
    log.info("Starting full reconciliation...")

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    v1net = client.NetworkingV1Api()
    custom = client.CustomObjectsApi()

    managed = get_managed_monitors(api)
    seen_keys = set()

    # --- Static monitors from ConfigMap ---
    static_keys = reconcile_static_monitors(api, managed, tag_id)
    seen_keys.update(static_keys)

    # --- Auto-discovered Kubernetes resources ---

    # Standard Ingress resources
    try:
        ingresses = v1net.list_ingress_for_all_namespaces()
        for ing in ingresses.items:
            resource = {
                "kind": "Ingress",
                "metadata": {
                    "name": ing.metadata.name,
                    "namespace": ing.metadata.namespace,
                    "annotations": ing.metadata.annotations or {},
                },
                "spec": client.ApiClient().sanitize_for_serialization(ing.spec),
            }
            key = build_monitor_key(resource)
            seen_keys.add(key)
            reconcile_resource(api, resource, managed, tag_id)
    except Exception as e:
        log.error("Error listing Ingresses: %s", e)

    # Traefik IngressRoute CRDs
    try:
        ingressroutes = custom.list_cluster_custom_object(
            "traefik.io", "v1alpha1", "ingressroutes"
        )
        for ir in ingressroutes.get("items", []):
            ir["kind"] = "IngressRoute"
            key = build_monitor_key(ir)
            seen_keys.add(key)
            reconcile_resource(api, ir, managed, tag_id)
    except Exception as e:
        log.debug("IngressRoute CRD not available: %s", e)

    # Gateway API HTTPRoute
    try:
        httproutes = custom.list_cluster_custom_object(
            "gateway.networking.k8s.io", "v1", "httproutes"
        )
        for hr in httproutes.get("items", []):
            hr["kind"] = "HTTPRoute"
            key = build_monitor_key(hr)
            seen_keys.add(key)
            reconcile_resource(api, hr, managed, tag_id)
    except Exception as e:
        log.debug("HTTPRoute CRD not available: %s", e)

    # Delete monitors for resources that no longer exist
    for key, monitor in managed.items():
        if key not in seen_keys:
            log.info("Deleting orphan monitor %s (resource gone)", key)
            try:
                api.delete_monitor(monitor["id"])
            except Exception as e:
                log.error("Failed to delete orphan monitor %s: %s", key, e)

    log.info(
        "Full reconciliation complete. Tracked %d resources (%d static, %d discovered).",
        len(seen_keys), len(static_keys), len(seen_keys) - len(static_keys),
    )


def watch_loop(api, tag_id):
    resync_interval = int(os.environ.get("RESYNC_INTERVAL", "300"))
    while not shutdown_event.is_set():
        try:
            full_reconcile(api, tag_id)
        except Exception as e:
            log.error("Reconciliation error: %s", e)
        shutdown_event.wait(timeout=resync_interval)


def main():
    kuma_url = os.environ["KUMA_URL"]
    username = os.environ["KUMA_USERNAME"]
    password = os.environ["KUMA_PASSWORD"]

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    while not shutdown_event.is_set():
        try:
            api = connect_kuma(kuma_url, username, password)
            tag_id = ensure_tag(api)
            watch_loop(api, tag_id)
        except Exception as e:
            log.error("Connection error: %s — retrying in 30s", e)
            shutdown_event.wait(timeout=30)

    log.info("Reconciler shut down.")


if __name__ == "__main__":
    main()
