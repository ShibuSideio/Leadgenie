import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_PIPELINE_ROOT = os.path.join(os.path.dirname(_HERE), "pipeline-main")
_PIPELINE_SERVICES = os.path.join(_PIPELINE_ROOT, "services")
if _PIPELINE_SERVICES not in sys.path:
    sys.path.insert(0, _PIPELINE_SERVICES)
if _PIPELINE_ROOT not in sys.path:
    sys.path.insert(0, _PIPELINE_ROOT)

from services.inbound_sentiment_service import InboundSentimentService
from services.inbound_maps_service import InboundMapsService
from gemini_service import pre_filter_gemini


class TestInboundRadarRemedies(unittest.TestCase):

    def test_url_pre_screen_filter(self):
        """Verify that _is_noise_url correctly identifies and filters directories, listicles, and competitor URLs."""
        svc = InboundSentimentService(
            persona={"competitors": ["competitorone", "competitor two"]},
            campaign={}
        )
        
        # Obvious directories / aggregators
        self.assertTrue(svc._is_noise_url("https://www.g2.com/products/competitor/reviews"))
        self.assertTrue(svc._is_noise_url("https://yelp.com/biz/local-business"))
        self.assertTrue(svc._is_noise_url("https://capterra.com/reviews/123"))
        
        # Path-based listicles / blogs / jobs
        self.assertTrue(svc._is_noise_url("https://example.com/blog/how-to-do-leads"))
        self.assertTrue(svc._is_noise_url("https://example.com/article/lead-gen-tips"))
        self.assertTrue(svc._is_noise_url("https://example.com/best-lead-generation-tools"))
        self.assertTrue(svc._is_noise_url("https://example.com/top-10-alternatives"))
        self.assertTrue(svc._is_noise_url("https://example.com/vs/competitor"))
        self.assertTrue(svc._is_noise_url("https://example.com/compare/competitor"))
        self.assertTrue(svc._is_noise_url("https://example.com/pricing"))
        self.assertTrue(svc._is_noise_url("https://example.com/login"))
        self.assertTrue(svc._is_noise_url("https://example.com/careers/sdr-opening"))
        
        # Competitor URL check
        self.assertTrue(svc._is_noise_url("https://competitorone.com"))
        self.assertTrue(svc._is_noise_url("https://competitortwo.com/features"))
        
        # Valid footprint URLs (niche blogs, customer forums, support tickets, help boards)
        self.assertFalse(svc._is_noise_url("https://nicheforum.net/threads/123-issues-with-billing"))
        self.assertFalse(svc._is_noise_url("https://github.com/org/repo/issues/456"))
        self.assertFalse(svc._is_noise_url("https://reddit.com/r/sales/comments/xyz"))
        self.assertFalse(svc._is_noise_url("https://www.facebook.com/groups/muscat/posts/123456"))

    @patch("gemini_service.call_gemini_2_5")
    def test_pre_filter_gemini_fallback(self, mock_call):
        """Verify that pre_filter_gemini falls back to python-based heuristic on exception."""
        mock_call.side_effect = Exception("Vertex AI Timeout")
        
        snippets = [
            {"link": "https://nicheforum.net/help/123", "title": "Help", "snippet": "Need alternative"},
            {"link": "https://g2.com/products/reviews", "title": "G2 Reviews", "snippet": "Spam reviews"},
            {"link": "https://example.com/blog/listicle", "title": "Blog", "snippet": "SEO post"},
            {"link": "https://github.com/org/repo/issues/1", "title": "Issue", "snippet": "Bug report"}
        ]
        
        res = pre_filter_gemini(snippets, "B2B SaaS", "US")
        
        # High tier should only contain the niche forum and the github issue
        self.assertIn("https://nicheforum.net/help/123", res["High"])
        self.assertIn("https://github.com/org/repo/issues/1", res["High"])
        # Obvious directories and blogs should be dropped
        self.assertNotIn("https://g2.com/products/reviews", res["High"])
        self.assertNotIn("https://example.com/blog/listicle", res["High"])

    @patch("services.inbound_maps_service.httpx.post")
    @patch("services.inbound_maps_service._get_serper_key")
    def test_fetch_place_reviews(self, mock_key, mock_post):
        """Verify reviews retrieval client parses Serper Reviews response correctly."""
        mock_key.return_value = "mock-key"
        
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "reviews": [
                {"name": "Alice", "rating": 1, "text": "Terrible customer support"},
                {"name": "Bob", "rating": 5, "text": "I loved it"}
            ]
        }
        mock_post.return_value = mock_resp
        
        svc = InboundMapsService(persona={}, campaign={})
        reviews = svc._fetch_place_reviews("mock-cid")
        
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[0]["name"], "Alice")
        self.assertEqual(reviews[0]["rating"], 1)


if __name__ == "__main__":
    unittest.main()
