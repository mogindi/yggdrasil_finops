import unittest
from unittest.mock import MagicMock

from billing_service import OpenSearchBillingRepository
from opensearch_client import OpenSearchApiError


class OpenSearchBillingRepositoryTests(unittest.TestCase):
    def test_invoice_crud_uses_opensearch(self):
        client = MagicMock()
        repo = OpenSearchBillingRepository(client)

        invoice = {"invoice_id": "inv_1", "project_id": "proj_1", "created_at": "2024-01-01T00:00:00+00:00"}
        repo.create_invoice("proj_1", invoice)

        ensure_call = client._http_json.call_args_list[0]
        self.assertEqual(ensure_call.args[0], "PUT")
        self.assertEqual(ensure_call.args[1], "/project-invoices")

        create_call = client._http_json.call_args_list[1]
        self.assertEqual(create_call.args[0], "PUT")
        self.assertEqual(create_call.args[1], "/project-invoices/_doc/inv_1")

        client._http_json.return_value = {"hits": {"hits": [{"_source": invoice}]}}
        listed = repo.list_invoices("proj_1")
        self.assertEqual(listed[0]["invoice_id"], "inv_1")

        found = repo.get_invoice("proj_1", "inv_1")
        self.assertEqual(found["invoice_id"], "inv_1")

    def test_delete_invoice_checks_project_and_deletes(self):
        client = MagicMock()
        repo = OpenSearchBillingRepository(client)
        client._http_json.side_effect = [
            {"hits": {"hits": [{"_source": {"invoice_id": "inv_1", "project_id": "proj_1"}}]}},
            {"result": "deleted"},
        ]

        deleted = repo.delete_invoice("proj_1", "inv_1")

        self.assertTrue(deleted)
        self.assertEqual(client._http_json.call_args_list[1].args[0], "DELETE")
        self.assertEqual(client._http_json.call_args_list[1].args[1], "/project-invoices/_doc/inv_1")

    def test_invoice_queries_return_empty_on_missing_index(self):
        client = MagicMock()
        repo = OpenSearchBillingRepository(client)
        missing_index = OpenSearchApiError(
            "OpenSearch request failed (404) method=GET url=http://opensearch:9200/project-invoices/_search: no such index [project-invoices]",
            status_code=404,
        )

        client._http_json.side_effect = missing_index
        self.assertEqual(repo.list_invoices("proj_1"), [])
        self.assertIsNone(repo.get_invoice("proj_1", "inv_1"))

    def test_receipt_queries_return_empty_on_missing_index(self):
        client = MagicMock()
        repo = OpenSearchBillingRepository(client)
        missing_index = OpenSearchApiError(
            "OpenSearch request failed (404) method=GET url=http://opensearch:9200/project-receipts/_search: no such index [project-receipts]",
            status_code=404,
        )

        client._http_json.side_effect = missing_index
        self.assertEqual(repo.list_receipts("proj_1"), [])
        self.assertIsNone(repo.get_receipt("proj_1", "rcpt_1"))

    def test_create_invoice_is_idempotent_when_index_exists(self):
        client = MagicMock()
        repo = OpenSearchBillingRepository(client)
        invoice = {"invoice_id": "inv_1", "project_id": "proj_1", "created_at": "2024-01-01T00:00:00+00:00"}
        client._http_json.side_effect = [
            OpenSearchApiError("resource exists", status_code=400, body='{"error":{"type":"resource_already_exists_exception"}}'),
            {"result": "created"},
        ]

        created = repo.create_invoice("proj_1", invoice)

        self.assertEqual(created["invoice_id"], "inv_1")
        self.assertEqual(client._http_json.call_args_list[0].args[1], "/project-invoices")
        self.assertEqual(client._http_json.call_args_list[1].args[1], "/project-invoices/_doc/inv_1")


if __name__ == "__main__":
    unittest.main()
