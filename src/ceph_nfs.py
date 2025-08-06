#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Handle Charm's NFS Client Events."""

import json
import logging
from typing import Callable

from charms.ceph_nfs_client.v0 import ceph_nfs_client
from ops.charm import CharmBase
from ops.framework import EventBase, Object
from ops_sunbeam.relation_handlers import RelationHandler

import ceph
import microceph
import utils
from microceph_client import Client

logger = logging.getLogger(__name__)


class CephNfsProviderHandler(RelationHandler):
    """Handler for the ceph-nfs relation."""

    def __init__(
        self,
        charm: CharmBase,
        relation_name: str,
        callback_f: Callable,
    ):
        super().__init__(charm, relation_name, callback_f)

    def setup_event_handler(self) -> Object:
        """Configure event handlers for an ceph-nfs-client interface."""
        logger.debug("Setting up ceph-nfs-client event handler")

        ceph_nfs = ceph_nfs_client.CephNfsClientProvides(
            self.charm,
            self.relation_name,
        )
        self.framework.observe(ceph_nfs.on.ceph_nfs_reconcile, self._on_ceph_nfs_reconcile)
        self.framework.observe(ceph_nfs.on.ceph_nfs_connected, self._on_ceph_nfs_connected)
        self.framework.observe(ceph_nfs.on.ceph_nfs_departed, self._on_ceph_nfs_departed)

        return ceph_nfs

    @property
    def ready(self) -> bool:
        """Check if handler is ready."""
        return True

    def _cluster_id(self, relation) -> str:
        return relation.app.name

    def _on_ceph_nfs_reconcile(self, event: EventBase) -> None:
        self._on_ceph_nfs_connected(event)

    def _on_ceph_nfs_connected(self, event: EventBase) -> None:
        if not self.model.unit.is_leader():
            return

        if ceph.get_osd_count() == 0:
            logger.info("Storage not available, deferring event.")
            event.defer()
            return

        logger.info("Processing ceph-nfs connected")
        if not self._service_relation(event.relation):
            logger.error("An error occurred while handling the ceph-nfs relation, deferring.")
            event.defer()

    def _service_relation(self, relation) -> bool:
        cluster_id = self._cluster_id(relation)
        relation_data = relation.data[self.model.app]

        if not self._ensure_nfs_cluster(cluster_id):
            # If we can't ensure even 1 node for the NFS cluster, clear the
            # relation data, as it wouldn't be usable.
            relation_data.clear()
            return False

        volume_name = f"{cluster_id}-vol"
        self._ensure_fs_volume(volume_name)

        client_name = f"client.{relation.app.name}"
        caps = {"mon": ["allow r"], "mgr": ["allow rw"]}
        client_key = ceph.get_named_key(client_name, caps)
        addrs = utils.get_mon_addresses()

        relation_data.update(
            {
                "fsid": utils.get_fsid(),
                "mon-hosts": json.dumps(addrs),
                "keyring": client_key,
                "volume": volume_name,
                "client": client_name,
                "cluster-id": cluster_id,
            }
        )

        return True

    def _ensure_nfs_cluster(self, cluster_id) -> bool:
        client = Client.from_socket()
        services = client.cluster.list_services()

        all_nfs_services = [s for s in services if s["service"] == "nfs"]
        nfs_services = [s for s in all_nfs_services if s.get("group_id") == cluster_id]
        nodes_in_cluster = len(nfs_services)

        if nodes_in_cluster >= 3:
            # We're only adding up to 3 nodes in the cluster.
            logger.info(
                "NFS Cluster '%s' already exists, and there are >= 3 nodes in it.", cluster_id
            )
            return True

        # Find potential candidates for the NFS cluster. We can only enable
        # NFS once per host.
        exclude_hosts = [s["location"] for s in all_nfs_services]

        all_hosts = set([s["location"] for s in services])
        candidates = [h for h in all_hosts if h not in exclude_hosts]

        for candidate in candidates:
            try:
                public_addr = self._get_public_address(candidate)
                if not public_addr:
                    logger.warning(
                        "Could not find the public address of '%s' in the peer relation data.",
                        candidate,
                    )
                    continue

                microceph.enable_nfs(candidate, cluster_id, public_addr)

                nodes_in_cluster += 1
                if nodes_in_cluster == 3:
                    break
            except Exception as ex:
                logger.error(
                    "Could not enable nfs (cluster_id '%s') on host '%s': %s",
                    cluster_id,
                    candidate,
                    ex,
                )

        if nodes_in_cluster == 0:
            logger.error("Could not create NFS Cluster '%s' on any host", cluster_id)
            return False

        if nodes_in_cluster < 3:
            logger.warning(
                "NFS cluster '%s' is enabled only on %d / 3 nodes.", cluster_id, nodes_in_cluster
            )
            return True

        logger.info("NFS cluster '%s' is enabled on 3 / 3 nodes.", cluster_id)
        return True

    def _get_public_address(self, hostname: str) -> str:
        rel = self.model.get_relation("peers")
        for unit in rel.units:
            rel_data = rel.data[unit]
            unit_hostname = rel_data.get(unit.name)

            if hostname == unit_hostname:
                return rel_data.get("public-address")

        return ""

    def _ensure_fs_volume(self, volume_name: str) -> None:
        """Create the FS Volume if it doesn't exist."""
        fs_volumes = ceph.list_fs_volumes()
        for fs_volume in fs_volumes:
            if fs_volume["name"] == volume_name:
                return

        ceph.create_fs_volume(volume_name)

    def _on_ceph_nfs_departed(self, event: EventBase) -> None:
        if not self.model.unit.is_leader():
            return

        logger.info("Processing ceph-nfs departed")

        cluster_id = self._cluster_id(event.relation)
        self._remove_nfs_cluster(cluster_id)

        client_name = f"client.{event.relation.app.name}"
        ceph.remove_named_key(client_name)

        # Because a relation departed, that means the nodes associated with it
        # are now free, which means that we can allocate them to the other NFS
        # clusters as needed.
        other_relations = [
            r for r in self.model.relations[self.relation_name] if r != event.relation
        ]
        for relation in other_relations:
            self._service_relation(relation)

    def _remove_nfs_cluster(self, cluster_id):
        client = Client.from_socket()
        services = client.cluster.list_services()
        nfs_services = [
            s for s in services if s["service"] == "nfs" and s.get("group_id") == cluster_id
        ]

        for service in nfs_services:
            host = service["location"]
            try:
                microceph.disable_nfs(host, cluster_id)
            except Exception as ex:
                logger.error(
                    "Could not disable nfs (cluster_id '%s') on host '%s': %s",
                    cluster_id,
                    host,
                    ex,
                )
                raise
