import unittest
from unittest.mock import MagicMock

from billing_service import OpenSearchBillingRepository


class OpenSearchBillingRepositoryTests(unittest.TestCase):
    def test_invoice_crud_uses_opensearch(self):
        client = MagicMock()
        repo = OpenSearchBillingRepository(client)

        invoice = {"invoice_id": "inv_1", "project_id": "proj_1", "created_at": "2024-01-01T00:00:00+00:00"}
        repo.create_invoice("proj_1", invoice)

        create_call = client._http_json.call_args_list[0]
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


if __name__ == "__main__":
    unittest.main()
