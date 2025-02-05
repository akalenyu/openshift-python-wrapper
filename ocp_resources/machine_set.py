import logging

from ocp_resources.resource import NamespacedResource
from ocp_resources.utils import TimeoutExpiredError, TimeoutSampler


LOGGER = logging.getLogger(__name__)


TIMEOUT = 300


class MachineSet(NamespacedResource):
    """
    Machineset object.

    Args:
        cluster_name (str): OpenShift cluster name.
        machine_role (str): machine role. e.g.: 'worker'.
        machine_type (str): machine role. e.g.: 'worker'.
        replicas (int): amount of replicas the machine-set will have.
        provider_spec (dict): provider spec information.

        provider spec example:

    {
        "value": {
            "apiVersion": "ovirtproviderconfig.machine.openshift.io/v1beta1",
            "auto_pinning_policy": "none",
            "cluster_id": "5612af70-f4f5-455d-b7df-fbad66accc38",
            "cpu": {
                "cores": 8,
                "sockets": 1,
                "threads": 1
            },
            "credentialsSecret": {
                "name": "ovirt-credentials"
            },
            "kind": "OvirtMachineProviderSpec",
            "memory_mb": 16000,
            "os_disk": {
                "size_gb": 31
            },
            "template_name": "ge2n1-gcwmg-rhcos",
            "type": "server",
            "userDataSecret": {
                "name": "worker-user-data"
            }
        }
    }
    """

    api_group = NamespacedResource.ApiGroup.MACHINE_OPENSHIFT_IO

    def __init__(
        self,
        cluster_name=None,
        name=None,
        namespace=None,
        teardown=True,
        client=None,
        machine_role="worker",
        machine_type="worker",
        replicas=1,
        provider_spec=None,
        yaml_file=None,
    ):
        super().__init__(
            name=name,
            namespace=namespace,
            client=client,
            teardown=teardown,
            yaml_file=yaml_file,
        )
        self.replicas = replicas
        self.cluster_name = cluster_name
        self.machine_role = machine_role
        self.machine_type = machine_type
        self.provider_spec = provider_spec or {}

    def to_dict(self):
        res = super().to_dict()
        if self.yaml_file:
            return res

        _spec, _metadata, _labels = ("spec", "metadata", "labels")
        (
            _cluster_api_cluster,
            _cluster_api_machine_role,
            _cluster_api_machine_type,
            _cluster_api_machineset,
        ) = (
            "cluster-api-cluster",
            "cluster-api-machine-role",
            "cluster-api-machine-type",
            "cluster-api-machineset",
        )

        res[_metadata][_labels] = {
            f"{self.api_group}/{_cluster_api_cluster}": self.cluster_name,
            f"{self.api_group}/{_cluster_api_machine_role}": self.machine_role,
            f"{self.api_group}/{_cluster_api_machine_type}": self.machine_type,
        }

        res[_spec] = {
            "replicas": self.replicas,
            "selector": {
                "matchLabels": {
                    f"{self.api_group}/{_cluster_api_cluster}": self.cluster_name,
                    f"{self.api_group}/{_cluster_api_machineset}": f"{self.cluster_name}-{self.machine_role}",
                }
            },
            "template": {
                _metadata: {
                    _labels: {
                        f"{self.api_group}/{_cluster_api_cluster}": self.cluster_name,
                        f"{self.api_group}/{_cluster_api_machine_role}": self.machine_role,
                        f"{self.api_group}/{_cluster_api_machine_type}": self.machine_type,
                        f"{self.api_group}/{_cluster_api_machineset}": f"{self.cluster_name}-{self.machine_role}",
                    }
                },
                _spec: {"providerSpec": self.provider_spec},
            },
        }
        return res

    @property
    def available_replicas(self):
        return self.instance.status.availableReplicas

    @property
    def ready_replicas(self):
        return self.instance.status.readyReplicas

    @property
    def desired_replicas(self):
        return self.instance.status.replicas

    @property
    def provider_spec_value(self):
        return self.instance.spec.template.spec.providerSpec.value

    def wait_for_replicas(self, timeout=TIMEOUT, sleep=1):
        """
        Wait for machine-set replicas to reach 'ready' state.

        Args:
            timeout (int): maximum time to wait_for_replicas for the 'ready' state.
            sleep (int): sleep time between each sample.

        Returns:
            bool: True if machine-set reached 'ready' state, False otherwise.
        """
        try:
            for ready_replicas in TimeoutSampler(
                wait_timeout=timeout, sleep=sleep, func=lambda: self.ready_replicas
            ):
                if ready_replicas and ready_replicas == self.desired_replicas:
                    return True
        except TimeoutExpiredError:
            LOGGER.error(
                f"Machine-set {self.name} replicas failed to reach into 'ready' state, actual ready replicas: "
                f"{self.ready_replicas}, desired replicas: {self.desired_replicas}"
            )
            return False

    def scale_replicas(self, replicas, wait_timeout=TIMEOUT, sleep=1, wait=True):
        """
        Scale down/up a machine-set replicas.

        Args:
            replicas (int): num of replicas to scale_replicas to.
            wait_timeout (int): maximum time to wait_for_replicas for scaling the machine-set.
            sleep (int): sleep time between each sample of the machine-set state.
            wait (bool): True if waiting for machine-set to reach into 'ready' state, False otherwise.

        Returns:
            bool: True if scaling the machine-set was successful or wait=False, False otherwise.
        """
        body = super().to_dict()
        body.update({"spec": {"replicas": replicas}})

        LOGGER.info(
            f"Scale machine-set from {self.desired_replicas} replicas to {replicas} replicas"
        )
        self.update(resource_dict=body)
        if wait:
            return self.wait_for_replicas(timeout=wait_timeout, sleep=sleep)
        return True
