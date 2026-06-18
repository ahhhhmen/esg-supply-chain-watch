from langchain_core.messages import AIMessage
from typing import Dict, Any
import logging

from ...classes import ResearchState
from .base import BaseResearcher

logger = logging.getLogger(__name__)

class CompanyAnalyzer(BaseResearcher):
    def __init__(self) -> None:
        super().__init__()
        self.analyst_type = "company_analyzer"

    async def analyze(self, state: ResearchState) -> Dict[str, Any]:
        company = state.get('company', 'Unknown Company')
        msg = [f"🏢 Company Analyzer analyzing {company}"]
        
        # Generate search queries using LLM (with exception handling)
        try:
            queries = await self.generate_queries(state, """
            Generate queries on the company fundamentals of {company} in the {industry} industry such as:
            - Core products and services
            - Company history and milestones
            - Leadership team
            - Business model and strategy
            """)
        except Exception as e:
            logger.error(f"Error generating queries for {self.analyst_type}: {e}")
            msg.append(f"\n⚠️ Error generating queries: {str(e)}")
            queries = self._fallback_queries(company, None)

        # Add message to show subqueries with emojis
        subqueries_msg = "🔍 Subqueries for company analysis:\n" + "\n".join([f"• {query}" for query in queries])
        messages = state.get('messages', [])
        messages.append(AIMessage(content=subqueries_msg))

    # Send queries through WebSocket
        if websocket_manager := state.get('websocket_manager'):
            if job_id := state.get('job_id'):
                await websocket_manager.send_status_update(
                    job_id=job_id,
                    status="processing",
                    message=f"Company analysis queries generated",
                    result={
                        "step": "Company Analyst",
                        "analyst_type": "Company Analyst",
                        "queries": queries
                    }
                )
        
        company_data = {}
        
        # If we have site_scrape data, include it first
        if site_scrape := state.get('site_scrape'):
            msg.append("\n📊 Including site scrape data in company analysis...")
            company_url = state.get('company_url', 'company-website')
            company_data[company_url] = {
                'title': state.get('company', 'Unknown Company'),
                'raw_content': site_scrape,
                'query': f'Company overview and information about {company}'  # Add a default query for site scrape
            }
        
        # Perform additional research with comprehensive search
        try:
            # Store documents with their respective queries
            for query in queries:
                documents = await self.search_documents(state, [query])
                if documents:  # Only process if we got results
                    for url, doc in documents.items():
                        doc['query'] = query  # Associate each document with its query
                        company_data[url] = doc
            
            msg.append(f"\n✓ Found {len(company_data)} documents")
            if websocket_manager := state.get('websocket_manager'):
                if job_id := state.get('job_id'):
                    await websocket_manager.send_status_update(
                        job_id=job_id,
                        status="processing",
                        message=f"Used Tavily Search to find {len(company_data)} documents",
                        result={
                            "step": "Searching",
                            "analyst_type": "Company Analyst",
                            "queries": queries
                        }
                    )
        except Exception as e:
            logger.error(f"Error during {self.analyst_type} research: {e}")
            msg.append(f"\n⚠️ Error during research: {str(e)}")
        
        # Return state updates (no in-place mutation)
        messages.append(AIMessage(content="\n".join(msg)))
        
        return {
            'messages': messages,
            'company_data': company_data
        }

    async def run(self, state: ResearchState) -> Dict[str, Any]:
        return await self.analyze(state) 