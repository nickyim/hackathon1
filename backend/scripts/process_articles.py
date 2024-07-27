from collections import Counter
import json
import os
import time
import requests
from bs4 import BeautifulSoup
import pinecone
from openai import OpenAI
from dotenv import load_dotenv
from langdetect import detect, LangDetectException
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(message)s')

# Load environment variables
load_dotenv()

PINECONE_API_KEY = os.getenv('PINECONE_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# Initialize Pinecone
pc = pinecone.Pinecone(api_key=PINECONE_API_KEY)

index_name = "news-articles"

# Create index if it doesn't exist
if index_name not in pc.list_indexes().names():
    pc.create_index(
        name=index_name,
        dimension=1536,
        metric='cosine',
        spec=pinecone.ServerlessSpec(
            cloud='aws',
            region='us-east-1'
        )
    )

index = pc.Index(index_name)

# Initialize OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# File to track the last processed URLs
LAST_PROCESSED_FILE = "last_processed.json"

# Function to load last processed URLs
def load_last_processed():
    if os.path.exists(LAST_PROCESSED_FILE):
        with open(LAST_PROCESSED_FILE, "r") as file:
            return json.load(file)
    return {}

# Function to save last processed URLs
def save_last_processed(last_processed):
    with open(LAST_PROCESSED_FILE, "w") as file:
        json.dump(last_processed, file)

# Function to get GDELT data with rate limiting and retry logic
def get_gdelt_data(query, maxrecords=250, retries=5):
    url = f"https://api.gdeltproject.org/api/v2/doc/doc?query={query}&mode=artlist&format=json&maxrecords={maxrecords}&lang=english"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36"
    }

    for i in range(retries):
        response = requests.get(url, headers=headers)

        if response.status_code == 200:
            try:
                return response.json()
            except requests.exceptions.JSONDecodeError:
                logging.error(f"Unable to parse JSON response. Response content: {response.content}")
                return {}
        elif response.status_code == 429:
            wait_time = 2 ** i  # Exponential backoff
            logging.warning(f"Rate limit exceeded. Retrying after {wait_time} seconds...")
            time.sleep(wait_time)
        else:
            logging.error(f"Received status code {response.status_code} from GDELT API")
            return {}

    logging.error("Maximum retries reached. Exiting.")
    return {}

# Function to extract article URLs
def extract_article_urls(data):
    articles = data.get("articles", [])
    urls = [article.get("url") for article in articles]
    return urls

# Function to scrape article content
def scrape_article_content(url):
    try:
        response = requests.get(url)
        soup = BeautifulSoup(response.content, 'html.parser')
        paragraphs = soup.find_all('p')
        content = ' '.join([para.get_text() for para in paragraphs])
        return content
    except Exception as e:
        logging.error(f"Error scraping content from {url}: {e}")
        return ""

# Function to get embeddings from OpenAI
def get_embeddings(text):
    try:
        response = client.embeddings.create(input=[text], model="text-embedding-ada-002")
        return response.data[0].embedding
    except Exception as e:
        logging.error(f"Error getting embeddings: {e}")
        return []

# Function to store articles in Pinecone with checks
def store_articles_batch(batch):
    try:
        response = index.upsert(vectors=batch)
        logging.info(f"Upserted batch of {len(batch)} articles. Pinecone response: {response}")
        return True
    except Exception as e:
        logging.error(f"Error upserting batch: {str(e)}")
        logging.error(f"Error type: {type(e).__name__}")
        logging.error(f"Error details: {e.args}")
        return False
    
# Function to process and store a single article
def process_and_store_article(url, content, categories, stored_urls, batch):
    # Check if content is in English
    try:
        if detect(content) != 'en':
            logging.info(f"Non-English content for URL {url}, skipping.")
            return False
    except LangDetectException as e:
        logging.info(f"Failed to detect language for URL {url}. Error: {e}")
        return False

    # Check for invalid content
    invalid_content_phrases = [
        "You don't have permission to access",
        "denied by UA ACL",
        "Performance & security by Cloudflare"
    ]
    if any(phrase in content for phrase in invalid_content_phrases) or not content.strip():
        logging.info(f"Invalid content for URL {url}: '{content.strip()}', skipping.")
        return False

    # Get embeddings
    embedding = get_embeddings(content)
    if not embedding:
        logging.info(f"Failed to get embeddings for URL {url}, skipping.")
        return False

    # New category matching logic
    content_lower = content.lower()
    category_scores = Counter()

    for category, terms in categories.items():
        score = sum(content_lower.count(term.lower()) for term in terms.replace('(', '').replace(')', '').split(' OR '))
        category_scores[category] = score

    # Find the categories with the highest score
    max_score = max(category_scores.values())
    matched_categories = [category for category, score in category_scores.items() if score == max_score]

    if not matched_categories:
        matched_categories = ["miscellaneous"]

    # Add to batch
    for category in matched_categories:
        if url not in stored_urls[category]:
            batch.append({"id": url, "values": embedding, "metadata": {"text": content, "category": category, "url": url}})
            stored_urls[category].add(url)

    logging.info(f"Assigned categories for {url}: {matched_categories}")
    return True

# Main function
def main():
    categories = {
        "finance": "(finance OR stock market OR investment OR bank OR economy OR recession OR inflation OR cryptocurrency)",
        "climate": "(climate change OR global warming OR extreme weather OR hurricane OR tornado OR flood OR drought OR wildfire OR carbon emissions)",
        "health": "(health OR medical OR healthcare OR medicine OR disease OR virus OR pandemic OR vaccine OR treatment OR hospital)",
        "sports": "(sports OR Olympics OR athletics OR athlete OR championship OR tournament OR league OR game OR match OR player)",
        "entertainment": "(entertainment OR movies OR music OR celebrity OR TV show OR concert OR festival OR award OR actor OR singer)",
        "politics": "(politics OR government OR election OR president OR congress OR senate OR law OR policy OR vote OR campaign)"
    }        
    stored_articles = 0
    maxrecords = 100  # Number of articles per request

    stored_urls = {category: set() for category in categories}
    stored_urls["miscellaneous"] = set()

    last_processed = load_last_processed()

    queries = list(categories.values())


    while True:
        for query in queries:
            data = get_gdelt_data(query, maxrecords)
            if not data:
                continue  # Skip if no data returned

            urls = extract_article_urls(data)
            batch = []

            for url in urls:
                if any(url in urls_set for urls_set in stored_urls.values()):
                    continue  # Skip already processed articles

                content = scrape_article_content(url)
                if content and process_and_store_article(url, content, categories, stored_urls, batch):
                    stored_articles += 1
                    logging.info(f"Stored articles count: {stored_articles}")

                    # Update the last processed URL
                    last_processed["last_processed_url"] = url

                # Upsert batch if it reaches the limit
                if len(batch) >= 10:  # Reduce the batch size to see data more frequently
                    if store_articles_batch(batch):
                        batch = []  # Reset the batch

                    save_last_processed(last_processed)  # Save progress after each batch
                    time.sleep(1)  # Add a delay to avoid rate limits

                    # Verify by querying Pinecone
                    query_response = index.query(vector=[0]*1536, top_k=2, include_metadata=True)
                    logging.info(f"Query response: {query_response}")

        # Upsert any remaining articles in the batch
        if batch:
            store_articles_batch(batch)

        save_last_processed(last_processed)  # Save progress after each batch
        time.sleep(1)  # Add a delay between each iteration to avoid rate limits

        logging.info(f"Total stored articles count: {stored_articles}")

if __name__ == "__main__":
    main()
