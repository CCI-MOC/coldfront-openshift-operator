import time
import secrets

import pytest
from allocation_operator import OpenShiftClient
from kopf.testing import KopfRunner
from kubernetes.dynamic.exceptions import NotFoundError


POLL_INTERVAL = 1
RESOURCE_TIMEOUT = 30


@pytest.fixture
def allocation_factory():
    def make_allocation(name, users=None, quota=None, on_delete_policy="Delete"):
        allocation_name = f"{name}-{secrets.token_hex(3)}" # To avoid conflicts in case of test failures and retries
        return {
            "apiVersion": "massopen.cloud/v1alpha1",
            "kind": "Allocation",
            "metadata": {"name": allocation_name},
            "spec": {
                "users": users or [f"{allocation_name}-user"],
                "quota": quota or {"requests.cpu": "2", "requests.memory": "4Gi"},
                "onDeletePolicy": on_delete_policy,
                "expirationDate": "2027-12-02",
            },
        }

    return make_allocation


@pytest.fixture
def client():
    with KopfRunner(args=["run", "--verbose", "allocation_operator.py"]) as runner:
        yield OpenShiftClient()

    assert runner.exit_code == 0
    assert runner.exception is None


def resource_api(client, api_version, kind):
    return client.get_resource_api(api_version, kind)


def get_resource(api, name, namespace=None):
    if namespace is None:
        return api.get(name=name).to_dict()
    return api.get(namespace=namespace, name=name).to_dict()


def wait_for_resource(api, name, namespace=None):
    for _ in range(RESOURCE_TIMEOUT):
        try:
            return get_resource(api, name, namespace)
        except NotFoundError:
            time.sleep(POLL_INTERVAL)
    pytest.fail(f"Resource {name!r} was not created within {RESOURCE_TIMEOUT} seconds")



def wait_for_resource_gone(api, name, namespace=None):
    for _ in range(RESOURCE_TIMEOUT):
        try:
            get_resource(api, name, namespace)
        except NotFoundError:
            return
        time.sleep(POLL_INTERVAL)
    pytest.fail(f"Resource {name!r} was not created within {RESOURCE_TIMEOUT} seconds")


def apply_allocation(client, allocation):
    allocation_api = resource_api(client, "massopen.cloud/v1alpha1", "Allocation")
    allocation_api.create(body=allocation)
    return allocation_api


def assert_allocation_resources(client, allocation):
    name = allocation["metadata"]["name"]
    users = allocation["spec"]["users"]
    quota = allocation["spec"]["quota"]
    resources = client.allocation_resources

    namespace = wait_for_resource(resources["namespace"], name)
    assert namespace["metadata"]["name"] == name

    group = wait_for_resource(resources["group"], name)
    assert group["users"] == users

    for username in users:
        user = wait_for_resource(resources["user"], username)
        assert user["fullName"] == username

    rolebinding = wait_for_resource(resources["rolebinding"], f"{name}-edit", name)
    assert rolebinding["roleRef"]["name"] == "edit"
    assert rolebinding["subjects"][0]["kind"] == "Group"
    assert rolebinding["subjects"][0]["name"] == name

    resource_quota = wait_for_resource(resources["quota"], f"{name}-project", name)
    assert resource_quota["spec"]["hard"] == quota

    limit_range = wait_for_resource(resources["limits"], f"{name}-limits", name)
    assert limit_range["spec"]["limits"] == [
        {
            "type": "Container",
            "default": {"cpu": "1", "memory": "4Gi", "nvidia.com/gpu": "0"},
            "defaultRequest": {"cpu": "500m", "memory": "2Gi", "nvidia.com/gpu": "0"},
            "min": {"cpu": "1m", "memory": "1Ki"},
        }
    ]


def test_delete_policy_removes_all_resources(client, allocation_factory):
    allocation = allocation_factory("allocation-delete", ["allocation-delete-user"])
    allocation_api = apply_allocation(client, allocation)
    assert_allocation_resources(client, allocation)

    allocation_api.delete(name=allocation["metadata"]["name"])
    resources = client.allocation_resources
    name = allocation["metadata"]["name"]
    wait_for_resource_gone(resources["namespace"], name)
    wait_for_resource_gone(resources["group"], name)
    wait_for_resource_gone(resources["user"], allocation["spec"]["users"][0])
    wait_for_resource_gone(resources["rolebinding"], f"{name}-edit", name)
    wait_for_resource_gone(resources["quota"], f"{name}-project", name)
    wait_for_resource_gone(resources["limits"], f"{name}-limits", name)


def test_archive_policy_removes_access_but_keeps_resources(client, allocation_factory):
    allocation = allocation_factory(
        "allocation-archive", ["allocation-archive-user"], on_delete_policy="Archive"
    )
    allocation_api = apply_allocation(client, allocation)
    assert_allocation_resources(client, allocation)

    allocation_api.delete(name=allocation["metadata"]["name"])
    name = allocation["metadata"]["name"]
    resources = client.allocation_resources
    wait_for_resource_gone(resources["rolebinding"], f"{name}-edit", name)
    assert get_resource(resources["namespace"], name)
    assert get_resource(resources["group"], name)


def test_ignore_policy_keeps_resources(client, allocation_factory):
    allocation = allocation_factory(
        "allocation-ignore", ["allocation-ignore-user"], on_delete_policy="Ignore"
    )
    allocation_api = apply_allocation(client, allocation)
    assert_allocation_resources(client, allocation)

    allocation_api.delete(name=allocation["metadata"]["name"])
    name = allocation["metadata"]["name"]
    resources = client.allocation_resources
    assert get_resource(resources["namespace"], name)
    assert get_resource(resources["rolebinding"], f"{name}-edit", name)


def test_timer_reconciles_deleted_quota_and_changed_group(client, allocation_factory):
    allocation = allocation_factory("allocation-reconcile", ["allocation-reconcile-user"])
    apply_allocation(client, allocation)
    assert_allocation_resources(client, allocation)

    resources = client.allocation_resources
    name = allocation["metadata"]["name"]
    quota_name = f"{name}-project"
    resources["quota"].delete(namespace=name, name=quota_name)

    group = get_resource(resources["group"], name)
    group["users"] = ["changed-user"]
    resources["group"].replace(name=name, body=group)

    time.sleep(10)  # Wait for the timer to trigger reconciliation

    recreated_quota = wait_for_resource(resources["quota"], quota_name, name)
    assert recreated_quota["spec"]["hard"] == allocation["spec"]["quota"]
    repaired_group = wait_for_resource(resources["group"], name)
    assert repaired_group["users"] == allocation["spec"]["users"]
