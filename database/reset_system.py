import os
import argparse
import psycopg2
from dotenv import load_dotenv
from neo4j import GraphDatabase
from .init_neo4j_schema import initialize_neo4j_schema

load_dotenv(override=True)


def reset_supabase():
    """Executes TRUNCATE on all document relations in Supabase."""
    uri = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_URI")
    if not uri:
        raise ValueError("Missing database connection URI in environment.")

    conn = psycopg2.connect(uri)
    cursor = conn.cursor()
    try:
        # DELETE (not TRUNCATE ... CASCADE): chunks go via the FK cascade, while the query_runs ledger
        # survives (its document_id is set to NULL instead of the table being emptied).
        print("[SUPABASE] Deleting all documents (chunks cascade; query_runs ledger is kept)...")
        cursor.execute("DELETE FROM documents;")
        conn.commit()
        print("[SUPABASE] Relational and vector store cleared (S_Pg -> Ø).")
    except Exception as e:
        conn.rollback()
        print(f"[SUPABASE ERROR] Reset failed: {e}")
        raise e
    finally:
        cursor.close()
        conn.close()


def reset_neo4j():
    """Executes DETACH DELETE on all graph nodes and edges in Neo4j."""
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD")

    if not all([uri, user, password]):
        raise ValueError("Missing Neo4j connection parameters in environment.")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            print("[NEO4J] Purging graph topology...")
            result = session.run("MATCH (n) DETACH DELETE n RETURN count(n) AS deleted_count;")
            record = result.single()
            deleted_nodes = record["deleted_count"] if record else 0
            print(f"[NEO4J] Deleted {deleted_nodes} nodes and associated edges (S_Neo4j -> Ø).")
    except Exception as e:
        print(f"[NEO4J ERROR] Reset failed: {e}")
        raise e
    finally:
        driver.close()


def execute_reset(target: str, confirm: bool):
    if not confirm:
        user_input = input(f"WARNING: This will permanently purge target '{target.upper()}'. Proceed? (y/N): ")
        if user_input.lower() != 'y':
            print("Reset operation aborted.")
            return

    print(f"--- INITIATING SYSTEM RESET (Target: {target.upper()}) ---")
    
    if target in ["all", "supabase"]:
        reset_supabase()

    if target in ["all", "neo4j"]:
        reset_neo4j()
        initialize_neo4j_schema()

    print(f"--- SYSTEM RESET COMPLETE ({target.upper()}) ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="System State Reset Utility")
    parser.add_argument(
        "--target",
        choices=["all", "neo4j", "supabase"],
        default="all",
        help="Specify which database state space to purge."
    )
    parser.add_argument(
        "--yes", 
        action="store_true", 
        help="Bypass interactive confirmation prompt."
    )
    args = parser.parse_args()
    execute_reset(target=args.target, confirm=args.yes)