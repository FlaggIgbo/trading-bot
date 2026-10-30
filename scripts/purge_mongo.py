"""
Simple script to purge (empty) the MongoDB database.
It connects to the MongoDB cluster and deletes all collections in the databases.
"""

import os
import certifi
from dotenv import load_dotenv
import pymongo

# Load .env file
load_dotenv()

def purge_database():
    mongo_uri = os.getenv("MONGODB_URI")
    if not mongo_uri:
        print("No MONGODB_URI found in environment or .env file.")
        return

    print("Connecting to MongoDB...")
    try:
        # Connect to MongoDB with a 10 second timeout
        client = pymongo.MongoClient(mongo_uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=10000)
        
        # Get list of all databases
        db_names = client.list_database_names()
        print(f"Found databases: {db_names}")

        # Drop non-system databases
        system_dbs = {"admin", "local", "config"}
        for db_name in db_names:
            if db_name in system_dbs:
                continue
            
            print(f"Purging database: {db_name}")
            db = client[db_name]
            for col_name in db.list_collection_names():
                print(f"  Dropping collection: {col_name}")
                db[col_name].drop()
            
            # Drop the database itself
            client.drop_database(db_name)
            print(f"Database {db_name} dropped.")

        print("Done! MongoDB cluster is completely empty.")

    except Exception as e:
        print(f"Could not connect or purge MongoDB: {e}")
        print("Note: If the cluster was inactive, MongoDB Atlas may have already shut down or deleted it.")

if __name__ == "__main__":
    purge_database()
