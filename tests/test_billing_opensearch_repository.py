import unittest
from unittest.mock import MagicMock, patch

from billing_service import OpenSearchBillingRepository


class OpenSearchBillingRepositoryTests(unittest.TestCase):
    @patch("billing_service.OpenSearchClient")
    def test_create_and_list_invoices_use_opensearch(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.search_project_invoices.return_value = {
            "hits": {
                "hits": [
                    {"_source": {"invoice_id": "inv_1", "project_id": "proj_1"}},
                    {"_source": {"invoice_id": "inv_2", "project_id": "proj_1"}},
                ]
            }
        }
        mock_client_cls.return_value = mock_client

        repo = OpenSearchBillingRepository()
        invoice = {"invoice_id": "inv_1", "project_id": "proj_1"}
        saved = repo.create_invoice("proj_1", invoice)
        rows = repo.list_invoices("proj_1")

        self.assertEqual(saved, invoice)
        self.assertEqual(len(rows), 2)
        mock_client.create_billing_indexes.assert_called_once()
        mock_client.upsert_invoice.assert_called_once_with("inv_1", invoice)
        mock_client.search_project_invoices.assert_called_once_with("proj_1")

    @patch("billing_service.OpenSearchClient")
    def test_get_invoice_returns_none_for_wrong_project(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get_invoice.return_value = {"_source": {"invoice_id": "inv_1", "project_id": "other"}}
        mock_client_cls.return_value = mock_client

        repo = OpenSearchBillingRepository()
        self.assertIsNone(repo.get_invoice("proj_1", "inv_1"))


if __name__ == "__main__":
    unittest.main()
