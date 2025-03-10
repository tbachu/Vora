import pandas as pd
import numpy as np
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from sklearn.preprocessing import StandardScaler
from sklearn.metrics.pairwise import cosine_similarity
from anthropic import Anthropic
from dotenv import load_dotenv
import os
import re
import json
from datetime import datetime
from fuzzywuzzy import fuzz
from fuzzywuzzy import process
from auth import requires_auth, AuthError
from pathlib import Path
from unc_scraper import UNCDiningScaper

# Load environment variables
load_dotenv()

print(f"API Key loaded: {'*' * (len(os.getenv('ANTHROPIC_API_KEY')) - 8)}{os.getenv('ANTHROPIC_API_KEY')[-8:]}")

app = Flask(__name__)
CORS(app)

# Initialize Anthropic client
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Load and preprocess the data
df = pd.read_csv('Data_prep.csv')

# Convert boolean columns to numeric and handle NaN values
boolean_columns = ['Vegan', 'Made Without Gluten', 'Vegetarian', 'Organic', 'Halal', 'Breakfast', 'Lunch', 'Dinner']
for col in boolean_columns:
    df[col] = df[col].map({'T': 1, 'F': 0, True: 1, False: 0}).fillna(0)

# Fill NaN values in numeric columns with mean
numeric_columns = ['Calories', 'Total Fat', 'Total Carbohydrates', 'Protein']
for col in numeric_columns:
    df[col] = df[col].fillna(df[col].mean())

# Create feature matrix for similarity calculation
feature_cols = ['Calories', 'Total Fat', 'Total Carbohydrates', 'Protein', 'Vegan', 
                'Made Without Gluten', 'Vegetarian', 'Organic', 'Halal']

# Initialize the scaler
features = df[feature_cols].values
scaler = StandardScaler()
features = scaler.fit_transform(features)

# Add ratings storage
ratings_db = {}  # Format: {user_id: [{meal_name: str, rating: int, date: str}]}

def load_ratings():
    """Load ratings from JSON file"""
    ratings_file = Path('ratings.json')
    if ratings_file.exists():
        try:
            with open(ratings_file, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

def save_ratings():
    """Save ratings to JSON file"""
    with open('ratings.json', 'w') as f:
        json.dump(ratings_db, f)

# Initialize ratings from file
ratings_db = load_ratings()

def extract_preferences_from_text(text):
    """Extract dietary preferences and restrictions from natural language input"""
    preferences = {
        'vegan': False,
        'vegetarian': False,
        'gluten_free': False,
        'halal': False,
        'target_calories': 2000,
        'target_protein': 50
    }
    
    # Convert text to lowercase for easier matching
    text = text.lower()
    
    # Check for dietary restrictions
    if any(word in text for word in ['vegan', 'plant-based', 'no animal']):
        preferences['vegan'] = True
        preferences['vegetarian'] = True
    
    if any(word in text for word in ['vegetarian', 'no meat']):
        preferences['vegetarian'] = True
    
    if any(word in text for word in ['gluten-free', 'gluten free', 'no gluten', 'celiac']):
        preferences['gluten_free'] = True
    
    if any(word in text for word in ['halal']):
        preferences['halal'] = True
    
    # Extract calorie targets
    calorie_match = re.search(r'(\d+)\s*(?:kcal|calories|cal)', text)
    if calorie_match:
        preferences['target_calories'] = int(calorie_match.group(1))
    
    # Extract protein targets
    protein_match = re.search(r'(\d+)\s*(?:g|grams)?\s*(?:of)?\s*protein', text)
    if protein_match:
        preferences['target_protein'] = int(protein_match.group(1))
    
    return preferences

def get_dietary_restrictions_text(food_item):
    """Get a formatted string of dietary restrictions for a food item"""
    restrictions = []
    if food_item['Vegan']:
        restrictions.append('Vegan')
    if food_item['Vegetarian']:
        restrictions.append('Vegetarian')
    if food_item['Made Without Gluten']:
        restrictions.append('Gluten-Free')
    if food_item['Halal']:
        restrictions.append('Halal')
    if food_item['Organic']:
        restrictions.append('Organic')
    return ', '.join(restrictions) if restrictions else 'None'

def get_meal_recommendations(preferences):
    """Generate meal recommendations based on user preferences with strict dietary restriction filtering"""
    try:
        # Create user preference vector
        user_pref = np.zeros(len(feature_cols))
        
        # Set dietary restrictions
        dietary_mapping = {
            'vegan': 'Vegan',
            'gluten_free': 'Made Without Gluten',
            'vegetarian': 'Vegetarian',
            'halal': 'Halal'
        }
        
        # Create a mask for filtering based on dietary restrictions
        valid_items_mask = np.ones(len(df), dtype=bool)
        
        for pref_key, feature_key in dietary_mapping.items():
            # Convert boolean values to bool type explicitly
            pref_value = bool(preferences.get(pref_key, False))
            if pref_value:
                user_pref[feature_cols.index(feature_key)] = 1
                # Strict filtering: only include items that match the dietary restriction
                valid_items_mask &= (df[feature_key] == 1)
                
                # If vegan is selected, also enforce vegetarian
                if pref_key == 'vegan':
                    valid_items_mask &= (df['Vegetarian'] == 1)
        
        # Set nutritional preferences with higher weights for dietary restrictions
        target_calories = float(preferences.get('target_calories', 2000)) / 3  # per meal
        target_protein = float(preferences.get('target_protein', 50)) / 3  # per meal
        
        # Set calorie and protein targets in the preference vector
        user_pref[feature_cols.index('Calories')] = target_calories
        user_pref[feature_cols.index('Protein')] = target_protein
        
        # Normalize user preferences
        user_pref_scaled = scaler.transform(user_pref.reshape(1, -1))[0]
        
        # Calculate similarity scores
        similarity_scores = cosine_similarity([user_pref_scaled], features)[0]
        
        # Apply dietary restrictions mask
        similarity_scores[~valid_items_mask] = -1
        
        # Get recommendations for each meal type
        meal_plan = {}
        for meal_type in ['Breakfast', 'Lunch', 'Dinner']:
            # Filter by meal type
            meal_mask = df[meal_type] == 1
            
            # Get top recommendations for each meal with some randomness
            meal_scores = similarity_scores.copy()
            meal_scores[~meal_mask] = -1
            
            # Add small random variation to scores to get different results each time
            valid_meals = meal_scores > -1
            meal_scores[valid_meals] += np.random.uniform(-0.1, 0.1, size=np.sum(valid_meals))
            
            # Get indices of top matches that satisfy all constraints
            top_indices = np.argsort(meal_scores)[-5:][::-1]  # Get top 5
            recommendations = []
            
            # Use all 5 top matches instead of randomly selecting 3
            for idx in top_indices:
                if meal_scores[idx] > -1:  # Only include valid recommendations
                    food_item = df.iloc[idx]
                    recommendations.append({
                        'name': str(food_item['Food Name']),
                        'calories': float(food_item['Calories']),
                        'protein': float(food_item['Protein']),
                        'carbs': float(food_item['Total Carbohydrates']),
                        'fat': float(food_item['Total Fat']),
                        'dietary_restrictions': get_dietary_restrictions_text(food_item)
                    })
            
            meal_plan[meal_type.lower()] = recommendations
        
        return meal_plan
    except Exception as e:
        print(f"Error in get_meal_recommendations: {str(e)}")
        return None

class ChatBot:
    def __init__(self):
        self.context = []
        self.system_prompt = """You are Vora, a food and nutrition assistant. Your purpose is to help users with food-related questions and meal planning.

CORE RULES:
1. ONLY discuss food and nutrition topics
2. If users ask about non-food topics, politely redirect them to food-related discussions
3. When suggesting meals, only recommend items from our food database
4. Focus on being helpful and informative about nutrition and meal choices
5. Format responses with clear spacing and bullet points for readability

RESPONSE FORMATTING RULES:
1. Add THREE empty lines between major sections (\\n\\n\\n)
2. Add TWO empty lines between subsections (\\n\\n)
3. Add ONE empty line between list items (\\n)
4. Use bullet points (•) for all list items
5. Indent sub-points with two spaces
6. Use emojis for section headers
7. Bold important text with **text**
8. Never write paragraphs - always use lists
9. Add horizontal lines (---) between major sections

EXAMPLE RESPONSE FORMAT:

👋 **Welcome!**\\n\\n\\n

🍽️ **Meal Suggestions**\\n\\n

• First meal option\\n
  • Detail 1\\n
  • Detail 2\\n\\n

• Second meal option\\n
  • Detail 1\\n
  • Detail 2\\n\\n\\n

💡 **Nutrition Tips**\\n\\n

• Tip 1\\n
  • Sub-detail\\n\\n

• Tip 2\\n
  • Sub-detail\\n\\n\\n

---"""

    def add_to_context(self, role: str, content: str):
        self.context.append({"role": role, "content": content})
        if len(self.context) > 10:
            self.context = self.context[-10:]

    def format_meal_summary(self, meal_plan):
        """Format meal plan summary with clear spacing and organization"""
        summary = "\\n\\n\\n-------------------------------------------\\n\\n\\n"
        summary += "📋 **Your Personalized Meal Plan**\\n\\n\\n"
        
        total_calories = 0
        total_protein = 0

        for meal_type in ['breakfast', 'lunch', 'dinner']:
            summary += f"🍽️ **{meal_type.capitalize()}**\\n\\n"
            
            for meal in meal_plan[meal_type]:
                summary += f"• {meal['name']}\\n"
                summary += f"  • Calories: {meal['calories']} cal\\n"
                summary += f"  • Protein: {meal['protein']}g\\n"
                if meal['dietary_restrictions'] != 'None':
                    summary += f"  • Suitable for: {meal['dietary_restrictions']}\\n"
                summary += "\\n"
                total_calories += meal['calories']
                total_protein += meal['protein']
            
            summary += "\\n\\n"

        summary += "-------------------------------------------\\n\\n\\n"
        
        summary += "📊 **Daily Nutrition Totals**\\n\\n"
        summary += f"• Total Calories: {total_calories} cal\\n"
        summary += f"• Total Protein: {total_protein}g\\n\\n\\n"

        summary += "💡 **Tips**\\n\\n"
        summary += "• View the complete meal plan in the Meal Plan tab\\n"
        summary += "• Click on meals to add them to your selection\\n"
        summary += "• Track your daily nutrition progress in real-time\\n\\n\\n"
        
        summary += "-------------------------------------------"
        return summary

    def generate_response(self, user_message: str) -> dict:
        try:
            # Extract preferences from user message
            preferences = extract_preferences_from_text(user_message)
            
            # Add user message to context
            self.add_to_context("user", user_message)
            
            # Get available foods for context
            available_foods = set(df['Food Name'].tolist())
            
            # Format context for Claude
            formatted_context = "\\n\\n\\n".join([
                f"{msg['role'].capitalize()}: {msg['content']}" 
                for msg in self.context[-5:]
            ])
            
            # Get response from Claude
            message = client.messages.create(
                model="claude-3-opus-20240229",
                max_tokens=2000,
                temperature=0.7,
                system=self.system_prompt,
                messages=[{
                    "role": "user",
                    "content": f"""Previous conversation:
                    {formatted_context}

                    Current message: {user_message}

                    Available foods (sample): 
                    {', '.join(list(available_foods)[:20])}... and more.

                    Remember to format your response with:
                    • THREE empty lines between major sections (\\n\\n\\n)
                    • TWO empty lines between subsections (\\n\\n)
                    • ONE empty line between list items (\\n)
                    • Bullet points for all items (•)
                    • Indentation for sub-points (two spaces)
                    • Never use paragraphs - always use lists
                    • Add horizontal lines between major sections (---)"""
                }]
            )
            
            # Extract the response text and ensure proper line breaks
            assistant_response = message.content[0].text if hasattr(message.content[0], 'text') else str(message.content)
            
            # Always generate a meal plan based on the conversation
            meal_plan = get_meal_recommendations(preferences)
            
            # If meal plan was generated, add it to the response
            if meal_plan:
                meal_summary = self.format_meal_summary(meal_plan)
                if "meal plan" not in assistant_response.lower():
                    assistant_response += "\\n\\n\\n" + meal_summary
            
            # Add assistant's response to context
            self.add_to_context("assistant", assistant_response)
            
            # Process the response to ensure proper line breaks
            processed_response = assistant_response.replace('\\n', '\n')
            
            return {
                "message": processed_response,
                "meal_plan": meal_plan,
                "extracted_preferences": preferences
            }
            
        except Exception as e:
            print(f"Error in generate_response: {str(e)}")
            import traceback
            traceback.print_exc()
            return {
                "message": "I apologize, but I encountered an error. Please try asking another food-related question.",
                "meal_plan": None,
                "extracted_preferences": None
            }

# Initialize chatbot
chatbot = ChatBot()

# Add this after other imports
scraper = UNCDiningScaper()

@app.route('/')
def home():
    return render_template('index.html')

@app.errorhandler(AuthError)
def handle_auth_error(ex):
    response = jsonify(ex.error)
    response.status_code = ex.status_code
    return response

@app.route('/get_meal_plan', methods=['POST'])
@requires_auth
def get_meal_plan():
    try:
        data = request.json
        preferences = {
            'vegan': data.get('vegan', False),
            'vegetarian': data.get('vegetarian', False),
            'gluten_free': data.get('gluten_free', False),
            'halal': data.get('halal', False),
            'target_calories': float(data.get('target_calories', 2000)),
            'target_protein': float(data.get('target_protein', 50))
        }
        
        meal_plan = get_meal_recommendations(preferences)
        if meal_plan is None:
            return jsonify({'error': 'Failed to generate meal plan'}), 500
            
        return jsonify(meal_plan)
    except Exception as e:
        print(f"Error in get_meal_plan: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/chat', methods=['POST'])
@requires_auth
def chat():
    try:
        data = request.json
        user_message = data.get('message', '')
        
        # Generate response using chatbot
        response = chatbot.generate_response(user_message)
        
        return jsonify(response)
        
    except Exception as e:
        print(f"Error in chat endpoint: {str(e)}")
        return jsonify({
            'error': str(e),
            'message': "I apologize, but I encountered an error while processing your request. "
                      "Could you please rephrase or try again?"
        }), 500

@app.route('/add_rating', methods=['POST'])
@requires_auth
def add_rating():
    try:
        data = request.json
        user_id = request.headers.get('X-User-Id')
        
        if not user_id:
            return jsonify({'error': 'User ID is required'}), 400
            
        meal_name = data.get('meal_name')
        rating = data.get('rating')
        
        if not meal_name or not isinstance(rating, int) or rating < 1 or rating > 5:
            return jsonify({'error': 'Invalid rating data'}), 400
            
        if user_id not in ratings_db:
            ratings_db[user_id] = []
            
        # Add new rating with timestamp
        ratings_db[user_id].append({
            'meal_name': meal_name,
            'rating': rating,
            'date': datetime.now().isoformat()
        })
        
        # Save ratings to file
        save_ratings()
        
        return jsonify({'message': 'Rating added successfully'})
    except Exception as e:
        print(f"Error in add_rating: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/get_user_ratings', methods=['GET'])
@requires_auth
def get_user_ratings():
    try:
        user_id = request.headers.get('X-User-Id')
        
        if not user_id:
            return jsonify({'error': 'User ID is required'}), 400
            
        user_ratings = ratings_db.get(user_id, [])
        
        # Get favorite meals (rated 4 or higher out of 5)
        favorites = [r for r in user_ratings if r['rating'] >= 4]
        favorites.sort(key=lambda x: x['rating'], reverse=True)
        
        return jsonify({
            'all_ratings': user_ratings,
            'favorites': favorites
        })
    except Exception as e:
        print(f"Error in get_user_ratings: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/scrape_menu', methods=['POST'])
@requires_auth
def scrape_menu():
    try:
        data = request.json
        url = data.get('url')
        
        if not url:
            return jsonify({'error': 'URL is required'}), 400
            
        # Validate URL format
        if not url.startswith(('http://', 'https://')):
            return jsonify({'error': 'Invalid URL format'}), 400
            
        # Update scraper URL and scrape
        scraper.base_url = url
        menu_data = scraper.scrape_menu()
        
        if menu_data is None:
            return jsonify({'error': 'Failed to scrape menu data'}), 500
            
        return jsonify({
            'message': 'Menu scraped successfully',
            'data': menu_data.to_dict('records')
        })
        
    except Exception as e:
        print(f"Error scraping menu: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
