# RUN BEFORE ingest.py 

import os
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv(override=True)

CYPHER_SCHEMA_STATEMENTS = [
    # 1. Unique Document IDs
    """
    CREATE CONSTRAINT document_id_unique IF NOT EXISTS 
    FOR (d:Document) REQUIRE d.id IS UNIQUE;
    """,
    
    # 2. Unique Chunk IDs
    """
    CREATE CONSTRAINT chunk_id_unique IF NOT EXISTS 
    FOR (c:Chunk) REQUIRE c.id IS UNIQUE;
    """,
    
    # 3. Unique Entity Names (Automates Rhizomatic Node Deduplication & Lookup Indexing)
    """
    CREATE CONSTRAINT entity_name_unique IF NOT EXISTS 
    FOR (e:Entity) REQUIRE e.name IS UNIQUE;
    """
]


def initialize_neo4j_schema():
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USERNAME")
    password = os.getenv("NEO4J_PASSWORD")

    if not all([uri, user, password]):
        raise ValueError("Missing Neo4j authentication parameters in .env file.")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    
    try:
        with driver.session() as session:
            print("[INFO] Initializing Neo4j Topological Constraints...")
            for statement in CYPHER_SCHEMA_STATEMENTS:
                session.run(statement)
            print("[SUCCESS] Schema constraints and indexes applied successfully.")
    except Exception as e:
        print(f"[ERROR] Failed to apply schema constraints: {e}")
        raise e
    finally:
        driver.close()


if __name__ == "__main__":
    initialize_neo4j_schema()