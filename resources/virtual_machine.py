# -*- coding: utf-8 -*-

import logging

import xmltodict
from openshift.dynamic.exceptions import ResourceNotFoundError
from resources.utils import TimeoutExpiredError, TimeoutSampler
from urllib3.exceptions import ProtocolError

from .node import Node
from .pod import Pod
from .resource import TIMEOUT, NamespacedResource


LOGGER = logging.getLogger(__name__)
API_GROUP = "kubevirt.io"


class VirtualMachine(NamespacedResource):
    """
    Virtual Machine object, inherited from Resource.
    Implements actions start / stop / status / wait for VM status / is running
    """

    api_group = API_GROUP

    def __init__(self, name, namespace, client=None, teardown=True):
        super().__init__(
            name=name, namespace=namespace, client=client, teardown=teardown
        )

    @property
    def _subresource_api_url(self):
        return (
            f"{self.client.configuration.host}/"
            f"apis/subresources.kubevirt.io/{self.api().api_version}/"
            f"namespaces/{self.namespace}/virtualmachines/{self.name}"
        )

    def to_dict(self):
        res = super().to_dict()
        res["spec"] = {"template": {"spec": {}}}
        return res

    def start(self, timeout=TIMEOUT, wait=False):
        self.client.client.request(
            "PUT",
            f"{self._subresource_api_url}/start",
            headers=self.client.configuration.api_key,
        )
        if wait:
            return self.wait_for_status(timeout=timeout, status=True)

    def restart(
        self, timeout=TIMEOUT, wait=False,
    ):
        self.client.client.request(
            "PUT",
            f"{self._subresource_api_url}/restart",
            headers=self.client.configuration.api_key,
        )
        if wait:
            self._wait_for_restart_status(timeout=timeout)
            return self.vmi.wait_until_running(timeout=timeout, stop_status="dummy")

    def _wait_for_restart_status(self, timeout=TIMEOUT):
        # stop_status="dummy" used to ignore FAILED/SUCCEEDED status during vmi restart
        # Note: if a VM + PCV has terminationGracePeriodSeconds > 0,
        # intermediate status will be SUCCEEDED instead of FAILED
        if self.vmi.instance.spec.get("terminationGracePeriodSeconds", 0) > 0 and any(
            [
                "persistentVolumeClaim" in volume.keys()
                for volume in self.vmi.instance.spec.volumes
            ]
        ):
            intermediate_status = self.Status.SUCCEEDED
        else:
            intermediate_status = self.Status.FAILED

        self.vmi.wait_for_status(
            status=intermediate_status, stop_status="dummy", timeout=timeout
        )

    def stop(self, timeout=TIMEOUT, wait=False):
        self.client.client.request(
            "PUT",
            f"{self._subresource_api_url}/stop",
            headers=self.client.configuration.api_key,
        )
        if wait:
            self.wait_for_status(timeout=timeout, status=None)
            return self.vmi.wait_deleted()

    def wait_for_status(self, status, timeout=TIMEOUT):
        """
        Wait for resource to be in status

        Args:
            status: Expected status: True for a running VM, None for a stopped VM.
            timeout (int): Time to wait for the resource.

        Raises:
            TimeoutExpiredError: If timeout reached.
        """
        LOGGER.info(f"Wait for {self.kind} {self.name} status to be {status}")
        samples = TimeoutSampler(
            timeout=timeout,
            sleep=1,
            exceptions=ProtocolError,
            func=self.api().get,
            field_selector=f"metadata.name=={self.name}",
            namespace=self.namespace,
        )
        for sample in samples:
            if sample.items:
                # VM with runStrategy does not have spec.running attribute
                # VM status should be taken from spec.status.ready
                if self.ready() == status:
                    return

    def get_interfaces(self):
        return self.instance.spec.template.spec.domain.devices.interfaces

    @property
    def vmi(self):
        """
        Get VMI

        Returns:
            VirtualMachineInstance: VMI
        """
        return VirtualMachineInstance(name=self.name, namespace=self.namespace,)

    def ready(self):
        """
        Get VM status

        Returns:
            True if Running else None
        """
        LOGGER.info(f"Check if {self.kind} {self.name} is ready")
        return self.instance.status["ready"] if self.instance.status else None


class VirtualMachineInstance(NamespacedResource):
    """
    Virtual Machine Instance object, inherited from Resource.
    """

    api_group = API_GROUP

    class Status(NamespacedResource.Status):
        RUNNING = "Running"
        SCHEDULING = "Scheduling"

    def __init__(self, name, namespace, client=None):
        super().__init__(name=name, namespace=namespace, client=client)

    @property
    def _subresource_api_url(self):
        return (
            f"{self.client.configuration.host}/"
            f"apis/subresources.kubevirt.io/{self.api().api_version}/"
            f"namespaces/{self.namespace}/virtualmachineinstances/{self.name}"
        )

    def to_dict(self):
        res = super().to_dict()
        return res

    def pause(self, timeout=TIMEOUT, wait=False):
        self.client.client.request(
            "PUT",
            f"{self._subresource_api_url}/pause",
            headers=self.client.configuration.api_key,
        )
        if wait:
            return self.wait_for_pause_status(pause=True, timeout=timeout)

    def unpause(self, timeout=TIMEOUT, wait=False):
        self.client.client.request(
            "PUT",
            f"{self._subresource_api_url}/unpause",
            headers=self.client.configuration.api_key,
        )
        if wait:
            return self.wait_for_pause_status(pause=False, timeout=timeout)

    @property
    def interfaces(self):
        return self.instance.status.interfaces

    @property
    def virt_launcher_pod(self):
        pods = list(
            Pod.get(
                dyn_client=self.client,
                namespace=self.namespace,
                label_selector=f"kubevirt.io=virt-launcher,kubevirt.io/created-by={self.instance.metadata.uid}",
            )
        )
        migration_state = self.instance.status.migrationState
        if migration_state:
            #  After VM migration there are two pods, one in Completed status and one in Running status.
            #  We need to return the Pod that is not in Completed status.
            for pod in pods:
                if migration_state.targetPod == pod.name:
                    return pod
        else:
            return pods[0]

        raise ResourceNotFoundError

    def wait_until_running(self, timeout=TIMEOUT, logs=True, stop_status=None):
        """
        Wait until VMI is running

        Args:
            timeout (int): Time to wait for VMI.
            logs (bool): True to extract logs from the VMI pod and from the VMI.
            stop_status (str): Status which should stop the wait and failed.

        Raises:
            TimeoutExpiredError: If VMI failed to run.
        """
        try:
            self.wait_for_status(
                status=self.Status.RUNNING, timeout=timeout, stop_status=stop_status
            )
        except TimeoutExpiredError:
            if not logs:
                raise

            virt_pod = self.virt_launcher_pod
            if virt_pod:
                LOGGER.debug(f"{virt_pod.name} *****LOGS*****")
                LOGGER.debug(virt_pod.log(container="compute"))

            raise

    def wait_for_pause_status(self, pause, timeout=TIMEOUT):
        """
        Wait for Virtual Machine Instance to be paused / unpaused.
        Paused status is checked in libvirt and in the VMI conditions.

        Args:
            pause (bool): True for paused, False for unpause
            timeout (int): Time to wait for the resource.

        Raises:
            TimeoutExpiredError: If resource not exists.
        """
        LOGGER.info(
            f"Wait until {self.kind} {self.name} is "
            f"{'Paused' if pause else 'Unpuased'}"
        )
        self.wait_for_domstate_pause_status(pause=pause, timeout=timeout)
        self.wait_for_vmi_condition_pause_status(pause=pause, timeout=timeout)

    def wait_for_domstate_pause_status(self, pause, timeout=TIMEOUT):
        pause_status = "paused" if pause else "running"
        samples = TimeoutSampler(
            timeout=timeout,
            sleep=1,
            exceptions=(ProtocolError),
            func=self.get_domstate,
        )
        for sample in samples:
            if pause_status in sample:
                return

    def wait_for_vmi_condition_pause_status(self, pause, timeout=TIMEOUT):
        samples = TimeoutSampler(
            timeout=timeout,
            sleep=1,
            exceptions=(ProtocolError),
            func=self.get_vmi_active_condition,
        )
        for sample in samples:
            # Paused VM
            if pause and sample["reason"] == "PausedByUser":
                return
            # Unpaused VM
            if not (pause and sample.get("reason")):
                return

    @property
    def node(self):
        """
        Get the node name where the VM is running

        Returns:
            Node: Node
        """
        return Node(name=self.instance.status.nodeName)

    def get_xml(self):
        """
        Get virtual machine instance XML

        Returns:
            xml_output(string): VMI XML in the multi-line string
        """
        return self.virt_launcher_pod.execute(
            command=["virsh", "dumpxml", f"{self.namespace}_{self.name}"],
            container="compute",
        )

    def get_domstate(self):
        """
        Get virtual machine instance Status.

        Current workaround, as VM/VMI shows no status/phase == Paused yet.
        Bug: https://bugzilla.redhat.com/show_bug.cgi?id=1805178

        Returns:
            String: VMI Status as string
        """
        return self.virt_launcher_pod.execute(
            command=["virsh", "domstate", f"{self.namespace}_{self.name}"],
            container="compute",
        )

    def get_vmi_active_condition(self):
        """ A VMI may have multiple conditions; the active one it the one with
        'lastTransitionTime' """
        return {
            k: v
            for condition in self.instance.status.conditions
            for k, v in condition.items()
            if condition["lastTransitionTime"]
        }

    @property
    def xml_dict(self):
        """ Get virtual machine instance XML as dict """

        return xmltodict.parse(self.get_xml(), process_namespaces=True)

    @property
    def guest_os_info(self):
        return self.instance.status.guestOSInfo

    @property
    def os_version(self):
        vmi_os_version = self.guest_os_info.get("version", {})
        if not vmi_os_version:
            LOGGER.warning(
                "Guest agent is not installed on the VM; OS version is not available."
            )
        return vmi_os_version

    def interface_ip(self, interface):
        iface_ip = [
            iface["ipAddress"]
            for iface in self.interfaces
            if iface["interfaceName"] == interface
        ]
        return iface_ip[0] if iface_ip else None


class VirtualMachineInstanceMigration(NamespacedResource):
    api_group = API_GROUP

    def __init__(self, name, namespace, vmi=None, client=None, teardown=True):
        super().__init__(
            name=name, namespace=namespace, client=client, teardown=teardown
        )
        self._vmi = vmi

    def to_dict(self):
        # When creating VirtualMachineInstanceMigration vmi is mandatory but when calling get()
        # we cannot pass vmi.
        assert self._vmi, "vmi is mandatory for create"

        res = super().to_dict()
        res["spec"] = {"vmiName": self._vmi.name}
        return res


class VirtualMachineInstancePreset(NamespacedResource):
    """
    VirtualMachineInstancePreset object.
    """

    api_group = API_GROUP


class VirtualMachineInstanceReplicaSet(NamespacedResource):
    """
    VirtualMachineInstancePreset object.
    """

    api_group = API_GROUP
