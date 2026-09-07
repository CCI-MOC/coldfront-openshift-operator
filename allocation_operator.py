"""Kopf operator for Mass Open Cloud Allocation resources."""

from __future__ import annotations

import functools
from typing import Any

import kopf

import kubernetes
import kubernetes.dynamic.exceptions as kexc
from openshift.dynamic import DynamicClient

GROUP = "massopen.cloud"
VERSION = "v1alpha1"
ALLOCATION_PLURAL = "allocations"
ROLE_NAME = "edit"

API_PROJECT = "project.openshift.io/v1"
API_USER = "user.openshift.io/v1"
API_RBAC = "rbac.authorization.k8s.io/v1"
API_CORE = "v1"

class OpenShiftClient:
    """A wrapper around the OpenShift dynamic client."""

    def __init__(self) -> None:
        k8s_client = kubernetes.config.new_client_from_config()
        self.dynamic_client = DynamicClient(k8s_client)

    def get_resource_api(self, api_version: str, kind: str):
        """Return the dynamic Kubernetes resource API for a given apiVersion and kind."""
        return self.dynamic_client.resources.get(api_version=api_version, kind=kind)

    @functools.cached_property
    def allocation_resources(self):
        return {
            "namespace": self.get_resource_api("v1", "Namespace"),
            "user": self.get_resource_api("user.openshift.io/v1", "User"),
            "group": self.get_resource_api("user.openshift.io/v1", "Group"),
            "rolebinding": self.get_resource_api("rbac.authorization.k8s.io/v1", "RoleBinding"),
            "quota": self.get_resource_api("v1", "ResourceQuota"),
            "limits": self.get_resource_api("v1", "LimitRange"),
        }


def _replace_or_create(
    resource_api: Any,
    name: str,
    resource: dict[str, Any],
    namespace: str | None = None,
) -> None:
    """Create a child resource, or replace it to repair changes made out of band."""
    try:
        if namespace is None:
            existing = resource_api.get(name=name)
        else:
            existing = resource_api.get(namespace=namespace, name=name)
    except kexc.NotFoundError:
        if namespace is None:
            resource_api.create(body=resource)
        else:
            resource_api.create(namespace=namespace, body=resource)
        return

    existing_metadata = existing.get("metadata", {})
    resource.setdefault("metadata", {})
    resource["metadata"]["resourceVersion"] = existing_metadata.get("resourceVersion")
    if namespace is None:
        resource_api.replace(name=name, body=resource)
    else:
        resource_api.replace(namespace=namespace, name=name, body=resource)


def _resource_quota(name: str, quota: dict[str, Any]) -> dict[str, Any]:
    return {
        "metadata": {"name": f"{name}-project"},
        "spec": {"hard": quota},
    }


def _limit_range(name: str) -> dict[str, Any]:
    return {
        "metadata": {"name": f"{name}-limits"},
        "spec": {
            "limits": [
                {   # Taken from coldfront-plugin-cloud
                    "type": "Container",
                    "default": {"cpu": "1", "memory": "4Gi", "nvidia.com/gpu": "0"},
                    "defaultRequest": {"cpu": "500m", "memory": "2Gi", "nvidia.com/gpu": "0"},
                    "min": {"cpu": "1m", "memory": "1Ki"},
                }
            ]
        },
    }


def _reconcile(name: str, spec: dict[str, Any], patch: kopf.Patch) -> None:
    users = spec.get("users", [])
    quota = spec.get("quota", {})

    patch["status"] = {"phase": "Progressing"}
    try:
        _reconcile_resources(name, users, quota)
    except Exception:
        patch["status"] = {"phase": "Failed"}
        raise

    patch["status"] = {"phase": "Ready"}


def _reconcile_resources(
    name: str,
    users: list[str],
    quota: dict[str, Any],
) -> None:
    resources = OpenShiftClient().allocation_resources

    namespace = {"metadata": {"name": name}}
    _replace_or_create(
        resources["namespace"],
        name,
        namespace,
    )

    group = {"metadata": {"name": name}, "users": users}
    _replace_or_create(
        resources["group"],
        name,
        group,
    )

    role_binding = {
        "metadata": {"name": f"{name}-edit"},
        "roleRef": {"kind": "ClusterRole", "name": ROLE_NAME},
        "subjects": [{"kind": "Group", "name": name}],
    }
    _replace_or_create(
        resources["rolebinding"],
        role_binding["metadata"]["name"],
        role_binding,
        namespace=name,
    )

    for username in users:
        user = {"metadata": {"name": username}, "fullName": username}
        _replace_or_create(
            resources["user"],
            username,
            user,
        )

    resource_quota = _resource_quota(name, quota)
    _replace_or_create(
        resources["quota"],
        name,
        resource_quota,
        namespace=name,
    )

    limit_range = _limit_range(name)
    _replace_or_create(
        resources["limits"],
        name,
        limit_range,
        namespace=name,
    )

@kopf.on.create(GROUP, VERSION, ALLOCATION_PLURAL)
def create_allocation(name: str, spec: dict[str, Any], patch: kopf.Patch, **_: Any) -> None:
    _reconcile(name, spec, patch)


@kopf.on.update(GROUP, VERSION, ALLOCATION_PLURAL)
def update_allocation(name: str, spec: dict[str, Any], patch: kopf.Patch, **_: Any) -> None:
    _reconcile(name, spec, patch)


@kopf.timer(GROUP, VERSION, ALLOCATION_PLURAL, interval=10.0)
def reconcile_allocation_periodically(name: str, spec: dict[str, Any], patch: kopf.Patch, **_: Any) -> None:
    _reconcile(name, spec, patch)


@kopf.on.delete(GROUP, VERSION, ALLOCATION_PLURAL)
def delete_allocation(name: str, spec: dict[str, Any], **_: Any) -> None:
    policy = spec.get("onDeletePolicy", "Delete")
    if policy == "Ignore":
        return

    client = OpenShiftClient()

    def safe_delete(resource_api_name: str, resource_name: str, object_name: str) -> None:
        resource_api = client.get_resource_api(resource_api_name, resource_name)
        try:
            if resource_name == "RoleBinding":
                resource_api.delete(name=object_name, namespace=name)
            else:
                resource_api.delete(name=object_name)
        except kexc.NotFoundError:
            pass

    safe_delete(API_RBAC, "RoleBinding", f"{name}-edit")

    if policy == "Archive":
        return

    safe_delete(API_USER, "Group", name)
    for username in spec.get("users", []):
        safe_delete(API_USER, "User", username)

    safe_delete(API_CORE, "Namespace", name)
