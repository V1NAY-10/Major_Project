from motor.motor_asyncio import AsyncIOMotorDatabase

class SessionVocabulary:
    """Allow users to define custom component names per session."""
    
    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
    
    async def define_term(self, session_id: str, term: str, cluster_id: str):
        """
        User says: "I call this part the 'arm'"
        System learns: "arm" → cluster_id in this session
        """
        await self.db.session_vocab.insert_one({
            "session_id": session_id,
            "term": term.lower(),
            "cluster_id": cluster_id
        })
    
    async def lookup_term(self, session_id: str, term: str) -> str | None:
        """Look up a custom term defined in this session."""
        doc = await self.db.session_vocab.find_one({
            "session_id": session_id,
            "term": term.lower()
        })
        return doc["cluster_id"] if doc else None
    
    async def enrich_clusters(self, session_id: str, clusters: list) -> list:
        """
        Enrich cluster data with session-specific aliases.
        """
        vocab = await self.db.session_vocab.find({"session_id": session_id}).to_list(None)
        
        cluster_map = {c["cluster_id"]: c for c in clusters}
        
        for entry in vocab:
            if entry["cluster_id"] in cluster_map:
                cluster_map[entry["cluster_id"]]["user_aliases"] = cluster_map[entry["cluster_id"]].get("user_aliases", [])
                cluster_map[entry["cluster_id"]]["user_aliases"].append(entry["term"])
        
        return list(cluster_map.values())
