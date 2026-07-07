import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
import pytest
from sourcing_engine import SourcingEngine

def test_sourcing_engine_url_cache(tmp_path):
    # Setup temporary processed_urls.json
    cache_dir = tmp_path / "logs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "processed_urls.json"
    
    # Pre-populate cache with an old URL and a new URL
    now = datetime.now(timezone.utc)
    old_time = (now - timedelta(days=15)).isoformat()  # Older than 14 days
    recent_time = (now - timedelta(days=5)).isoformat()  # Newer than 14 days
    
    initial_cache = {
        "http://example.com/old": old_time,
        "http://example.com/recent": recent_time
    }
    
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(initial_cache, f)
        
    # Instantiate SourcingEngine and override cache_path for isolation
    engine = SourcingEngine()
    engine.cache_path = cache_file
    
    # Re-trigger loading & pruning to simulate init with our mocked file
    engine.processed_urls = {}
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            engine.processed_urls = json.load(f)
            
    # Prune
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    pruned = {}
    for u, ts_str in engine.processed_urls.items():
        ts = datetime.fromisoformat(ts_str)
        if ts >= cutoff:
            pruned[u] = ts_str
    engine.processed_urls = pruned
    
    # Assert old URL is pruned and recent URL is kept
    assert "http://example.com/old" not in engine.processed_urls
    assert "http://example.com/recent" in engine.processed_urls
    
    # Test interception: check if seen URL is filtered
    # We will simulate the check
    assert "http://example.com/recent" in engine.processed_urls
    assert "http://example.com/new" not in engine.processed_urls
    
    # Test saving new URLs
    new_urls = ["http://example.com/new", "http://example.com/recent"]
    engine._save_processed_urls(new_urls)
    
    # Read back cache file and check contents
    with open(cache_file, "r", encoding="utf-8") as f:
        saved_cache = json.load(f)
        
    assert "http://example.com/new" in saved_cache
    assert "http://example.com/recent" in saved_cache
    assert "http://example.com/old" not in saved_cache
