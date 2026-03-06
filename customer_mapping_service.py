import datetime as dt
import threading
from dataclasses import dataclass

from opensearch_client import OpenSearchClient


@dataclass
class CustomerProjectMapping:
    customer_id: str
    project_ids: list[str]


class InMemoryCustomerProjectRepository:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, set[str]] = {}

    def get(self, customer_id: str) -> CustomerProjectMapping:
        with self._lock:
            projects = sorted(self._items.get(customer_id, set()))
        return CustomerProjectMapping(customer_id=customer_id, project_ids=projects)

    def add_project(self, customer_id: str, project_id: str) -> CustomerProjectMapping:
        with self._lock:
            self._items.setdefault(customer_id, set()).add(project_id)
        return self.get(customer_id)

    def remove_project(self, customer_id: str, project_id: str) -> CustomerProjectMapping:
        with self._lock:
            self._items.setdefault(customer_id, set()).discard(project_id)
        return self.get(customer_id)


class OpenSearchCustomerProjectRepository:
    def __init__(self, client: OpenSearchClient) -> None:
        self._client = client

    def get(self, customer_id: str) -> CustomerProjectMapping:
        payload = self._client.get_customer_project_mapping(customer_id)
        source = payload.get("_source", {})
        project_ids = source.get("project_ids", [])
        if not isinstance(project_ids, list):
            project_ids = []
        return CustomerProjectMapping(customer_id=customer_id, project_ids=sorted({str(p) for p in project_ids if p}))

    def add_project(self, customer_id: str, project_id: str) -> CustomerProjectMapping:
        mapping = self.get(customer_id)
        projects = sorted(set(mapping.project_ids) | {project_id})
        self._client.put_customer_project_mapping(customer_id, {
            "customer_id": customer_id,
            "project_ids": projects,
            "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        })
        return self.get(customer_id)

    def remove_project(self, customer_id: str, project_id: str) -> CustomerProjectMapping:
        mapping = self.get(customer_id)
        projects = [pid for pid in mapping.project_ids if pid != project_id]
        self._client.put_customer_project_mapping(customer_id, {
            "customer_id": customer_id,
            "project_ids": projects,
            "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        })
        return self.get(customer_id)
