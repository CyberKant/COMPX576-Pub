| Variable Name | Used By | Purpose | Safe for Client |
| :--- | :--- | :--- | :--- |
| DATABASE_URL | Backend | Supabase Postgres transaction pooler URI for direct database connections | No |
| SUPABASE_KEY | Backend | Supabase public/anon API key | No |
| SUPABASE_URL | Backend | Supabase project API base URL | No |
| SUPABASE_SERVICE_ROLE_KEY | Backend | Supabase administrative service role key (bypasses RLS) | No |
| NEO4J_URI | Backend | Neo4j AuraDB graph instance connection URI | No |
| NEO4J_USERNAME | Backend | Neo4j instance authentication username | No |
| NEO4J_PASSWORD | Backend | Neo4j instance authentication password | No |
| GEMINI_API_KEY | Backend | Google Gemini API key for embeddings and response generation and extraction | No |
| GROQ_API_KEY | Backend | Groq API key for high-throughput topological entity/relation extraction (deprecated in our setup) | No |
| GEMINI_EMBEDDING_MODEL | Backend | String identifier of the embedding model | Yes |
| GEMINI_EXTRACTION_MODEL | Backend | String identifier of the generation/extraction model | Yes |