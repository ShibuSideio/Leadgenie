import os
import sys
import json
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

# Mock services.telemetry in sys.modules to prevent path resolution failures when testing pipeline modules
sys.modules["services.telemetry"] = MagicMock()

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

    @patch("services.inbound_sentiment_service.httpx.post")
    @patch("services.inbound_sentiment_service._get_serper_key")
    def test_inbound_sentiment_service_timeframe(self, mock_key, mock_post):
        """Verify Serper payload contains appropriate tbs date filters."""
        mock_key.return_value = "mock-key"
        
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"organic": []}
        mock_post.return_value = mock_resp
        
        # 1. Default timeframe should be qdr:y (past year)
        svc = InboundSentimentService(persona={}, campaign={})
        svc._search_serper("test query")
        
        args, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        self.assertEqual(payload.get("tbs"), "qdr:y")
        
        # 2. Overridden timeframe should propagate
        svc_custom = InboundSentimentService(persona={}, campaign={"inbound_timeframe": "qdr:m"})
        svc_custom._search_serper("test query")
        
        args, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        self.assertEqual(payload.get("tbs"), "qdr:m")

        # 3. Timeframe "all" should omit tbs
        svc_all = InboundSentimentService(persona={}, campaign={"inbound_timeframe": "all"})
        svc_all._search_serper("test query")
        
        args, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        self.assertNotIn("tbs", payload)

    @patch("serper_service.httpx.post")
    @patch("serper_service._get_serper_api_key")
    def test_pipeline_serper_service_timeframe(self, mock_key, mock_post):
        """Verify pipeline search_serper applies tbs date filters conditionally."""
        mock_key.return_value = "mock-key"
        
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"organic": []}
        mock_post.return_value = mock_resp
        
        from serper_service import search_serper
        
        # 1. B2C campaign should have tbs="qdr:y"
        search_serper("test query", sourcing_vector="B2C")
        args, kwargs = mock_post.call_args
        payload = json.loads(kwargs.get("data", "{}"))
        self.assertEqual(payload.get("tbs"), "qdr:y")
        
        # 2. B2B campaign should not have tbs
        search_serper("test query", sourcing_vector="B2B")
        args, kwargs = mock_post.call_args
        payload = json.loads(kwargs.get("data", "{}"))
        self.assertNotIn("tbs", payload)

    def test_inbound_sentiment_service_dialog_cue_dorking(self):
        """Verify that _build_queries appends dialog cues only for B2C/consumer campaigns."""
        # 1. B2C campaign
        svc_b2c = InboundSentimentService(
            persona={"pain_points": ["rent villa"]},
            campaign={"sourcing_vector": "B2C"}
        )
        queries_b2c = svc_b2c._build_queries()
        self.assertTrue(len(queries_b2c) > 0)
        for q in queries_b2c:
            self.assertIn('"pm me" OR "pm sent" OR "still available"', q)
            
        # 2. B2B campaign
        svc_b2b = InboundSentimentService(
            persona={"pain_points": ["rent villa"]},
            campaign={"sourcing_vector": "B2B"}
        )
        queries_b2b = svc_b2b._build_queries()
        self.assertTrue(len(queries_b2b) > 0)
        for q in queries_b2b:
            self.assertNotIn('"pm me" OR "pm sent" OR "still available"', q)

    @patch("vertexai.generative_models.GenerativeModel")
    @patch("core.clients.init_vertex")
    def test_inbound_sentiment_service_context_aware_llm(self, mock_init, mock_model_cls):
        """Verify that _score_with_gemini includes query context in prompt instructions."""
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_resp = MagicMock()
        mock_resp.text = '{"intent_label": "ACTIVE_SEEKING", "intent_score": 0.9, "matched_pain_keywords": [], "company_name": null, "industry_hint": null, "reasoning": "Fits search query"}'
        mock_model.generate_content.return_value = mock_resp
        
        from services.inbound_sentiment_service import _score_with_gemini
        
        res = _score_with_gemini("Test Title", "Test Snippet", "https://example.com", "Oman villa pm me", "ICP description")
        
        self.assertIsNotNone(res)
        self.assertEqual(res["intent_label"], "ACTIVE_SEEKING")
        args, kwargs = mock_model.generate_content.call_args
        prompt = args[0]
        self.assertIn("Triggering Google Query: Oman villa pm me", prompt)
        self.assertIn("CONTEXT-AWARE CONVERSATIONAL INFERENCE", prompt)


if __name__ == "__main__":
    unittest.main()
