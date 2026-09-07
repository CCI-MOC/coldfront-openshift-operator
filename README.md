# ColdFront OpenShift Allocation Operator

This repository contains a [Kopf](https://github.com/nolar/kopf)-based OpenShift operator for provisioning ColdFront allocations.
It is intended to move allocation provisioning out of [`coldfront-plugin-cloud`](https://github.com/nerc-project/coldfront-plugin-cloud) and
into a Kubernetes controller. This lets ColdFront manage an `Allocation` custom resource
instead of directly managing namespaces, users, groups, role bindings, quotas, and limit ranges.

The operator watches `Allocation` resources and creates or repairs the corresponding OpenShift
resources. This reduces the OpenShift permissions required by ColdFront and keeps provisioned
resources aligned with the allocation specification.

## Files of interest

- `allocation_operator.py`: Kopf operator and OpenShift client abstraction.
- `k8s/allocation-crd.yaml`: `Allocation` CRD definition.
- `tests/test_openshift_operator.py`: functional tests that run against a live OpenShift cluster.

## Install the CRD

The cluster must have the OpenShift APIs used by the operator, including the OpenShift `User` and
`Group` resources. Install the CRD with:

```bash
oc apply -f k8s/allocation-crd.yaml
```

Confirm that it is available:

```bash
oc get crd allocations.massopen.cloud
```

## Run the Operator

Create a Python environment and install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

With `KUBECONFIG` pointing to the target cluster, start the operator from the repository root:

```bash
kopf run --verbose allocation_operator.py
```

## Allocation CRD

The CRD is cluster-scoped and uses API version `massopen.cloud/v1alpha1`:

```yaml
apiVersion: massopen.cloud/v1alpha1
kind: Allocation
metadata:
	name: research-allocation
spec:
	users:
		- dylan
		- bob
	quota:
		requests.cpu: 16
		requests.memory: 32Gi
		limits.cpu: "16"
		limits.memory: 64Gi
		persistentvolumeclaims: "10"
	onDeletePolicy: Delete
	expirationDate: "2027-12-02"
```

### Spec Fields

- `users` is a required, non-empty list of OpenShift usernames. The operator creates one OpenShift
	`User` object for each entry and adds the users to the allocation `Group`.
- `quota` is a required map of resource quota keys to integer or string values. It is used as the
	`spec.hard` value of the allocation's `ResourceQuota`.
- `onDeletePolicy` is required and must be `Delete`, `Archive`, or `Ignore`:
	- `Delete` removes the role binding, group, users, and namespace when the Allocation is deleted.
	- `Archive` removes the role binding but retains the other managed resources.
	- `Ignore` leaves managed resources untouched.
- `expirationDate` is an optional date in `YYYY-MM-DD` format. It is stored on the resource, but
	expiration handling is not currently implemented.

### Managed Resources

For an allocation named `research-allocation`, the operator maintains:

- Namespace `research-allocation`.
- OpenShift Group `research-allocation` containing the configured users.
- One OpenShift User per configured username.
- Namespaced RoleBinding `research-allocation-edit` granting the `edit` ClusterRole to the group.
- Namespaced ResourceQuota `research-allocation-project` using the configured quota.
- Namespaced LimitRange `research-allocation-limits` with the operator's default container limits.

The Allocation status reports one of these phases: `Progressing`, `Failed`, or `Ready`.

Apply an allocation with:

```bash
oc apply -f allocation.yaml
oc get allocation research-allocation -o yaml
```

## Functional Tests

Functional tests require a live OpenShift or MicroShift cluster, and a
`KUBECONFIG` that can create the resources managed by the operator. 
[`pytest-xdist`](https://pytest-xdist.readthedocs.io/en/stable/) is used to run tests in paralell

Install the CRD and dependencies, then run:

```bash
oc apply -f k8s/allocation-crd.yaml
pip install -r requirements.txt
pytest -n auto tests
```

Each test allocation receives a random suffix, so the functional tests can run in parallel without
sharing allocation, namespace, quota, or user names.

For local MicroShift setup, the repository includes:

```bash
./ci/setup-microshift.sh
```

That script starts the MicroShift container, prepares `KUBECONFIG`, waits for the cluster, and
installs the Allocation CRD. The script requires Docker, `oc`, and the permissions needed to run
the container setup commands.
