import os
from pathlib import Path
import psycopg2
from neo4j import GraphDatabase
from dotenv import load_dotenv

# Initialize the environment space by loading variables from the project root .env file
# This enforces the Domain Constraint: Zero hardcoded credentials.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)


def get_postgres_connection():
    """
    Establishes a TCP/IP connection to the Supabase PostgreSQL instance.
    Returns a psycopg2 connection object if successful, None otherwise.
    """
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        print("ERROR: DATABASE_URL not found in .env space.")
        return None

    try:
        # Initialize the Postgres client handle
        conn = psycopg2.connect(database_url)
        return conn
    except Exception as e:
        print(f"PostgreSQL connection failed: {e}")
        return None


def get_neo4j_driver():
    """
    Establishes a connection pool to the Neo4j AuraDB instance using the Bolt protocol.
    Returns a neo4j driver object if successful, None otherwise.
    """
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USERNAME")
    password = os.getenv("NEO4J_PASSWORD")

    if not all([uri, user, password]):
        print("ERROR: Neo4j credentials missing in .env space.")
        return None

    try:
        # Initialize the Neo4j client handle
        driver = GraphDatabase.driver(uri, auth=(user, password))
        return driver
    except Exception as e:
        print(f"Neo4j driver initialization failed: {e}")
        return None


if __name__ == "__main__":
    print("--- Testing Database Connection Boundaries ---")

    # 1. Verify PostgreSQL Traversal
    print("\nTesting PostgreSQL (Supabase) Connection...")
    pg_conn = get_postgres_connection()
    if pg_conn:
        try:
            # Execute a lightweight query to verify active state space
            cur = pg_conn.cursor()
            cur.execute("SELECT version();")
            version = cur.fetchone()
            print(f"SUCCESS: Connected to {version[0]}")

            # Close cursor and connection to prevent memory leaks
            cur.close()
            pg_conn.close()
        except Exception as e:
            print(f"FAILED: Connection established but query execution failed: {e}")
    else:
        print("FAILED: Could not establish PostgreSQL boundary.")

    # 2. Verify Neo4j Traversal
    print("\nTesting Neo4j (AuraDB) Connection...")
    neo4j_driver = get_neo4j_driver()
    if neo4j_driver:
        try:
            # Force the driver to perform a network handshake
            neo4j_driver.verify_connectivity()
            print("SUCCESS: Established secure Bolt protocol connection to Neo4j AuraDB.")

            # Close the driver
            neo4j_driver.close()
        except Exception as e:
            print(f"FAILED: Network handshake rejected by Neo4j: {e}")
    else:
        print("FAILED: Could not establish Neo4j boundary.")

    print("\n--- Verification Complete ---")
