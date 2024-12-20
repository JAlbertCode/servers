from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types
import asyncio
import httpx
import json
import bs4
import time
from datetime import datetime
from dataclasses import dataclass
from typing import List, Dict, Optional
import os
from pathlib import Path

# Data structures for tracking
@dataclass
class Company:
    name: str
    website: str
    type: str  # 'sponsor' or 'attendee'
    first_seen: datetime
    last_seen: datetime

@dataclass
class Contact:
    name: str
    title: str
    company: str
    email: str
    apollo_id: str

class EventTracker:
    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self.companies: Dict[str, Company] = {}
        self.contacts: Dict[str, Contact] = {}
        self.last_check = None
        self.load_data()

    def load_data(self):
        if self.storage_path.exists():
            data = json.loads(self.storage_path.read_text())
            self.companies = {
                k: Company(**v) for k, v in data.get('companies', {}).items()
            }
            self.contacts = {
                k: Contact(**v) for k, v in data.get('contacts', {}).items()
            }
            self.last_check = data.get('last_check')

    def save_data(self):
        data = {
            'companies': {k: v.__dict__ for k, v in self.companies.items()},
            'contacts': {k: v.__dict__ for k, v in self.contacts.items()},
            'last_check': self.last_check
        }
        self.storage_path.write_text(json.dumps(data, default=str))

# Initialize server
server = Server("event-tracker")
tracker = EventTracker(Path("event_data.json"))

# Apollo.io API client
class ApolloClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.apollo.io/v1"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    async def search_people(self, company_name: str, seniority_levels: List[str]) -> List[dict]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/mixed_people/search",
                headers=self.headers,
                json={
                    "api_key": self.api_key,
                    "q_organization_name": company_name,
                    "person_titles": ["CEO", "CTO", "CFO", "CMO", "President", "VP", "Director"],
                    "seniority": seniority_levels
                }
            )
            return response.json()['people']

    async def add_to_sequence(self, sequence_id: str, contact_ids: List[str]):
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/sequences/add_contacts",
                headers=self.headers,
                json={
                    "api_key": self.api_key,
                    "sequence_id": sequence_id,
                    "contact_ids": contact_ids
                }
            )
            return response.json()

apollo_client = ApolloClient(os.getenv("APOLLO_API_KEY"))

# Tool definitions
@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="scan-event-website",
            description="Scan a website for event sponsors and attendees",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Event website URL"
                    }
                },
                "required": ["url"]
            }
        ),
        types.Tool(
            name="enrich-contacts",
            description="Find key decision makers at companies using Apollo.io",
            inputSchema={
                "type": "object",
                "properties": {
                    "sequence_id": {
                        "type": "string",
                        "description": "Apollo.io sequence ID to add contacts to"
                    }
                },
                "required": ["sequence_id"]
            }
        ),
        types.Tool(
            name="get-changes",
            description="Get changes in sponsors and attendees since last check",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        )
    ]

async def extract_companies(url: str) -> List[Company]:
    """Extracts companies from the event website."""
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        soup = bs4.BeautifulSoup(response.text, 'html.parser')
        
        companies = []
        # This is a simplified example - you'd need to adjust selectors for the actual website
        for sponsor in soup.select('.sponsor'):
            companies.append(Company(
                name=sponsor.select_one('.name').text,
                website=sponsor.select_one('a')['href'],
                type='sponsor',
                first_seen=datetime.now(),
                last_seen=datetime.now()
            ))
            
        for attendee in soup.select('.attendee'):
            companies.append(Company(
                name=attendee.select_one('.name').text,
                website=attendee.select_one('a')['href'],
                type='attendee',
                first_seen=datetime.now(),
                last_seen=datetime.now()
            ))
            
        return companies

@server.call_tool()
async def handle_call_tool(
    name: str,
    arguments: dict | None
) -> list[types.TextContent]:
    if name == "scan-event-website":
        if not arguments or 'url' not in arguments:
            raise ValueError("URL is required")
            
        url = arguments['url']
        companies = await extract_companies(url)
        
        # Update tracker
        now = datetime.now()
        for company in companies:
            if company.name in tracker.companies:
                tracker.companies[company.name].last_seen = now
            else:
                tracker.companies[company.name] = company
                
        tracker.last_check = now
        tracker.save_data()
        
        return [
            types.TextContent(
                type="text",
                text=f"Found {len(companies)} companies: " + 
                     ", ".join(c.name for c in companies)
            )
        ]

    elif name == "enrich-contacts":
        if not arguments or 'sequence_id' not in arguments:
            raise ValueError("Apollo.io sequence ID is required")
            
        sequence_id = arguments['sequence_id']
        new_contacts = []
        
        # Search for contacts at each company
        for company in tracker.companies.values():
            try:
                people = await apollo_client.search_people(
                    company.name,
                    ["director", "executive", "vp"]
                )
                
                for person in people:
                    contact = Contact(
                        name=person['name'],
                        title=person['title'],
                        company=company.name,
                        email=person['email'],
                        apollo_id=person['id']
                    )
                    tracker.contacts[contact.apollo_id] = contact
                    new_contacts.append(contact)
                    
                # Rate limiting
                await asyncio.sleep(1)
                    
            except Exception as e:
                server.request_context.session.send_log_message(
                    level="error",
                    data=f"Error enriching {company.name}: {str(e)}"
                )
                
        # Add to sequence
        if new_contacts:
            try:
                await apollo_client.add_to_sequence(
                    sequence_id,
                    [c.apollo_id for c in new_contacts]
                )
            except Exception as e:
                server.request_context.session.send_log_message(
                    level="error",
                    data=f"Error adding to sequence: {str(e)}"
                )
                
        tracker.save_data()
        
        return [
            types.TextContent(
                type="text",
                text=f"Added {len(new_contacts)} contacts to sequence"
            )
        ]

    elif name == "get-changes":
        if not tracker.last_check:
            return [
                types.TextContent(
                    type="text",
                    text="No previous scan to compare with"
                )
            ]
            
        companies = await extract_companies(url)
        current_names = {c.name for c in companies}
        previous_names = set(tracker.companies.keys())
        
        new_companies = current_names - previous_names
        removed_companies = previous_names - current_names
        
        return [
            types.TextContent(
                type="text",
                text=f"Changes since {tracker.last_check}:\n" +
                     f"New: {', '.join(new_companies)}\n" +
                     f"Removed: {', '.join(removed_companies)}"
            )
        ]

    else:
        raise ValueError(f"Unknown tool: {name}")

async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="event-tracker",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    asyncio.run(main())