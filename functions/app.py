from quart import Quart, render_template, jsonify, request, abort, Response, redirect
import pandas as pd
from collections import Counter
from datetime import datetime, timezone, timedelta
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from functions.clean_html import clean_html
import motor.motor_asyncio  # <-- ADDED
import os                   # <-- ADDED
import re                   # <-- ADDED
from urllib.parse import urlparse
from copy import deepcopy
from config import *
# --- NEW MONGODB SETUP ---
MONGO_DB_URL = MONGO_URI
if not MONGO_DB_URL:
    print("CRITICAL: MONGO_DB_URL environment variable not set.")
    exit()
    
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_DB_URL)
db = mongo_client["rainy_season_db"]  # Your database name
print("Dashboard app connected to MongoDB.")
# --- END MONGODB SETUP ---

FILTER_CACHE_TTL_SECONDS = 45
dashboard_filter_cache = {}
DEFAULT_FILTER_WINDOW_DAYS = 30
PRECOMPUTED_FILTER_INTERVALS = ("daily", "weekly", "monthly", "lifetime")

# --- Helper functions are UNCHANGED ---
def format_timestamp_relative(iso_string):
    # ... (your function is unchanged) ...
    if iso_string:
        try:
            dt_obj = datetime.fromisoformat(iso_string)
            if dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=timezone.utc)
            now_dt = datetime.now(timezone.utc)
            time_difference = now_dt - dt_obj
            seconds = int(time_difference.total_seconds())
            minutes = seconds // 60
            hours = minutes // 60
            days = hours // 24
            months = days // 30
            years = days // 365
            if seconds < 60:
                return "just now" if seconds == 0 else f"{seconds} secs ago"
            elif minutes < 60:
                return f"{minutes} mins ago"
            elif hours < 24:
                return f"{hours} hours ago"
            elif days < 30:
                return f"{days} days ago"
            elif months < 12:
                return f"{months} months ago"
            else:
                return f"{years} years ago"
        except ValueError:
            pass
    return 'N/A'
        
# --- process_data is UNCHANGED ---
# It already accepts lists/dicts, so it doesn't care about the database.
def process_data(chat_logs, server_info, boosters, emojis_json, members_list):
    # ... (all your existing pandas/processing logic is 100% fine) ...
    chat_df = pd.DataFrame(chat_logs)
    # (This function is exactly the same as yours)
    if not chat_df.empty:
        chat_df['timestamp'] = pd.to_datetime(chat_df['timestamp'], errors='coerce', utc=True)
        chat_df['day'] = chat_df['timestamp'].dt.dayofweek
        chat_df['hour'] = chat_df['timestamp'].dt.hour
    else:
        chat_df = pd.DataFrame(columns=['timestamp', 'day', 'hour', 'date', 'channel', 'user_id', 'author', 'avatar', 'content', 'emojis'])
        chat_df['timestamp'] = pd.Series(dtype='datetime64[ns, UTC]')
        chat_df['day'] = pd.Series(dtype='int64')
        chat_df['hour'] = pd.Series(dtype='int64')
        chat_df['date'] = pd.Series(dtype='object')
        chat_df['user_id'] = pd.Series(dtype='object')
        chat_df['author'] = pd.Series(dtype='object')
        chat_df['avatar'] = pd.Series(dtype='object')
        chat_df['content'] = pd.Series(dtype='object')
        chat_df['emojis'] = pd.Series(dtype='object')

    total_messages = len(chat_df)
    total_users = server_info.get('total_users', 0)
    active_users = server_info.get('active_users', 0)
    rain_keepers = server_info.get('rain_keeper_count', 0)
    updated_at_iso = server_info.get('updated_at')
    updated_at_text = format_timestamp_relative(updated_at_iso) # Use your own filter!
    
    member_profile_map = {
        str(m.get('id')): {
            'display_name': m.get('display_name', m.get('name', 'Unknown User')),
            'name': m.get('name', 'Unknown User'),
            'avatar': m.get('avatar', 'default.png')
        }
        for m in members_list
    }

    if not chat_df.empty:
        chat_df['date'] = chat_df['timestamp'].dt.date
        activity_over_time = chat_df.groupby('date').size().reset_index(name='messages')
        channel_activity = chat_df['channel'].value_counts().reset_index()
        channel_activity.columns = ['channel', 'count']
        user_counts = chat_df.groupby(['user_id']).size().reset_index(name='messages')
        top_users_df = user_counts.sort_values(by='messages', ascending=False).head(6)
        top_users = []
        for row in top_users_df.to_dict('records'):
            user_id = str(row.get('user_id'))
            profile = member_profile_map.get(user_id, {})
            top_users.append({
                'user_id': user_id,
                'name': profile.get('display_name') or profile.get('name') or 'Unknown User',
                'avatar': profile.get('avatar', 'default.png'),
                'messages': int(row.get('messages', 0))
            })
        if 'emojis' in chat_df.columns and not chat_df['emojis'].empty:
            all_emojis_flat = [emoji for emoji_list in chat_df['emojis'].dropna() for emoji in emoji_list]
            emoji_usage = Counter(all_emojis_flat)
        else:
            emoji_usage = Counter()
    else:
        activity_over_time = pd.DataFrame(columns=['date', 'messages'])
        channel_activity = pd.DataFrame(columns=['channel', 'count'])
        top_users = []
        emoji_usage = Counter()
    
    sentiment_data = []
    if not chat_df.empty and 'content' in chat_df.columns:
        analyzer = SentimentIntensityAnalyzer()
        def calculate_vader_sentiment(text):
            if not isinstance(text, str) or not text.strip(): return 0.0
            return analyzer.polarity_scores(text)['compound']
        content_df = chat_df.dropna(subset=['content']).copy()
        if not content_df.empty:
            content_df['sentiment_score'] = content_df['content'].apply(calculate_vader_sentiment)
            daily_sentiment = content_df.groupby(content_df['timestamp'].dt.date)['sentiment_score'].mean().reset_index()
            daily_sentiment.columns = ['date', 'sentiment']
            sentiment_data = daily_sentiment.to_dict('records')

    custom_emoji_map = {}
    for emoji in emojis_json:
        clean_name = emoji['name'].replace(':', '').lower()
        custom_emoji_map[clean_name] = emoji
        custom_emoji_map[str(emoji.get('id'))] = emoji
    top_emojis = []
    for emoji_name, count in emoji_usage.most_common(10):
        matched_emoji = custom_emoji_map.get(emoji_name.lower()) or custom_emoji_map.get(emoji_name)
        if matched_emoji:
            top_emojis.append({'name': matched_emoji.get('name', emoji_name), 'count': count, 'url': matched_emoji.get('url'), 'id': matched_emoji.get('id'), 'is_unicode': False, 'animated': matched_emoji.get('animated', False)})
        else:
            top_emojis.append({'name': emoji_name, 'count': count, 'is_unicode': True})

    activity_list = list(activity_over_time.to_dict('records'))
    channel_activity_list = list(channel_activity.to_dict('records'))
    top_emojis_list = list(top_emojis)
    heatmap_matrix = [[0] * 24 for _ in range(7)]
    if not chat_df.empty:
        grouped_by_day_hour = chat_df.groupby(['day', 'hour']).size()
        for idx, count in grouped_by_day_hour.items():
            if isinstance(idx, tuple) and len(idx) == 2:
                day, hour = idx
                try:
                    day_int = int(day); hour_int = int(hour)
                    if 0 <= day_int < 7 and 0 <= hour_int < 24:
                        heatmap_matrix[day_int][hour_int] = count
                except (TypeError, ValueError): pass
    
    normalized_boosters = []
    for booster in boosters:
        profile = member_profile_map.get(str(booster.get('id')), {})
        normalized_boosters.append({
            'id': str(booster.get('id')),
            'name': profile.get('display_name') or booster.get('name', 'Unknown User'),
            'avatar': profile.get('avatar', booster.get('avatar', 'default.png')),
            'boosted_since': booster.get('boosted_since')
        })

    active_streaks_data = []
    if not chat_df.empty:
        streak_df = chat_df[['user_id', 'timestamp']].dropna().copy()
        streak_df['date'] = streak_df['timestamp'].dt.date
        latest_data_date = streak_df['date'].max() if not streak_df.empty else None
        user_activity_dates = streak_df.groupby('user_id')['date'].apply(lambda x: sorted(list(x.unique())))
        for user_id, dates in user_activity_dates.items():
            longest_streak = 0; current_streak = 0
            if not dates: continue
            temp_longest = 0
            for i in range(len(dates)):
                if i == 0 or (dates[i] - dates[i-1]).days != 1: temp_longest = 1
                else: temp_longest += 1
                longest_streak = max(longest_streak, temp_longest)
            if latest_data_date and dates[-1] == latest_data_date:
                temp_current = 1
                for i in range(len(dates) - 2, -1, -1):
                    if (dates[i+1] - dates[i]).days == 1: temp_current += 1
                    else: break
                current_streak = temp_current
            else: current_streak = 0 
            user_info = member_profile_map.get(str(user_id), {'display_name': 'Unknown User', 'avatar': 'default.png'})
            active_streaks_data.append({'user_id': user_id, 'display_name': user_info['display_name'], 'avatar': user_info['avatar'], 'longest_streak': longest_streak, 'current_streak': current_streak})
    active_streaks_data.sort(key=lambda x: (x['longest_streak'], x['current_streak']), reverse=True)
    active_streaks_data = active_streaks_data[:10] 

    activity_insights = {
        'messages_today': 0,
        'messages_yesterday': 0,
        'messages_last_7_days': 0,
        'daily_average': 0,
        'active_days': 0,
        'busiest_day_label': 'N/A',
        'busiest_day_messages': 0,
        'most_active_hour_label': 'N/A',
        'most_active_hour_messages': 0,
        'channel_count': 0,
    }

    if not chat_df.empty:
        current_utc_date = datetime.now(timezone.utc).date()
        previous_data_date = current_utc_date - timedelta(days=1)
        seven_day_start = current_utc_date - timedelta(days=6)
        daily_counts = chat_df.groupby('date').size()
        hourly_counts = chat_df.groupby('hour').size()

        busiest_day = daily_counts.idxmax()
        busiest_day_messages = int(daily_counts.max())
        most_active_hour = int(hourly_counts.idxmax())
        most_active_hour_messages = int(hourly_counts.max())

        activity_insights = {
            'messages_today': int(daily_counts.get(current_utc_date, 0)),
            'messages_yesterday': int(daily_counts.get(previous_data_date, 0)),
            'messages_last_7_days': int(daily_counts[daily_counts.index >= seven_day_start].sum()),
            'daily_average': round(float(daily_counts.mean()), 1),
            'active_days': int(daily_counts.shape[0]),
            'busiest_day_label': busiest_day.strftime('%b %d, %Y'),
            'busiest_day_messages': busiest_day_messages,
            'most_active_hour_label': f"{most_active_hour:02d}:00 UTC",
            'most_active_hour_messages': most_active_hour_messages,
            'channel_count': int(chat_df['channel'].nunique()),
        }
    
    return {
        'server': server_info, 'boosters': normalized_boosters, 'messages': total_messages,
        'total_users': total_users, 'active_users': active_users,
        'updated_at_text': updated_at_text, 'rain_keepers': rain_keepers,
        'top_users_data': top_users, 'activity': activity_list,
        'channel_activity': channel_activity_list, 'top_emojis': top_emojis_list,
        'heatmap': heatmap_matrix, 'sentiment_data': sentiment_data,
        'active_streaks_data': active_streaks_data,
        'activity_insights': activity_insights
    }

def aggregate_timeseries(records, interval, value_key):
    if not records:
        return []

    grouped = {}
    for record in records:
        raw_date = record.get('date')
        if not raw_date:
            continue
        if isinstance(raw_date, str):
            current_date = datetime.fromisoformat(raw_date).date()
        else:
            current_date = raw_date

        if interval == 'weekly':
            bucket_date = current_date - timedelta(days=current_date.weekday())
        elif interval == 'monthly':
            bucket_date = current_date.replace(day=1)
        else:
            bucket_date = current_date

        bucket = grouped.setdefault(bucket_date, {'total': 0.0, 'count': 0})
        bucket['total'] += float(record.get(value_key, 0) or 0)
        bucket['count'] += 1

    results = []
    for bucket_date in sorted(grouped.keys()):
        bucket = grouped[bucket_date]
        if value_key == 'sentiment':
            value = round(bucket['total'] / bucket['count'], 4) if bucket['count'] else 0
        else:
            value = int(bucket['total'])
        results.append({'date': bucket_date.isoformat(), value_key: value})
    return results


def sanitize_processed_stats_for_storage(processed_stats):
    for key in ("activity", "sentiment_data"):
        if key not in processed_stats:
            continue
        for item in processed_stats[key]:
            if "date" in item and hasattr(item["date"], "isoformat"):
                item["date"] = item["date"].isoformat()
    return processed_stats


def build_filter_definition(interval, start_date, end_date):
    return {
        "interval": interval,
        "start": start_date,
        "end": end_date,
        "start_iso": start_date.isoformat(),
        "end_iso": end_date.isoformat(),
        "label": "Lifetime" if interval == "lifetime" else f"{start_date.strftime('%b %d, %Y')} - {end_date.strftime('%b %d, %Y')}",
    }


def get_default_filter_range(today=None):
    current_day = today or datetime.now(timezone.utc).date()
    return current_day - timedelta(days=DEFAULT_FILTER_WINDOW_DAYS - 1), current_day


def build_standard_filter_definitions(today=None):
    current_day = today or datetime.now(timezone.utc).date()
    lifetime_start = datetime(2015, 1, 1, tzinfo=timezone.utc).date()
    return {
        "daily": build_filter_definition("daily", current_day, current_day),
        "weekly": build_filter_definition("weekly", current_day - timedelta(days=6), current_day),
        "monthly": build_filter_definition("monthly", current_day - timedelta(days=29), current_day),
        "lifetime": build_filter_definition("lifetime", lifetime_start, current_day),
    }


def get_standard_filter_key(interval, start_date, end_date, today=None):
    if interval == "lifetime":
        return "lifetime"

    default_start, default_end = get_default_filter_range(today)
    if start_date == default_start and end_date == default_end and interval in PRECOMPUTED_FILTER_INTERVALS:
        return interval
    return None


def finalize_filtered_dashboard_stats(filtered_stats, filters):
    sanitize_processed_stats_for_storage(filtered_stats)
    filtered_stats["activity"] = aggregate_timeseries(filtered_stats.get("activity", []), filters["interval"], "messages")
    filtered_stats["sentiment_data"] = aggregate_timeseries(filtered_stats.get("sentiment_data", []), filters["interval"], "sentiment")
    filtered_stats["filter_state"] = {
        "interval": filters["interval"],
        "start": filters["start_iso"],
        "end": filters["end_iso"],
        "label": filters["label"],
    }
    filtered_stats["filtered_window"] = {
        "messages": int(filtered_stats.get("messages", 0)),
        "top_members_count": len(filtered_stats.get("top_users_data", [])),
        "streak_members_count": len(filtered_stats.get("active_streaks_data", [])),
    }
    return filtered_stats

def create_app():
    app = Quart(
        __name__,
        template_folder="../templates",
        static_folder="../static",
        static_url_path="/static",
    )

    # --- Jinja Custom Filters (Unchanged) ---
    def hex_to_rgb(hex_color):
        if not hex_color: return None
        try:
            hex_color = str(hex_color).lstrip('#') 
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            return f"{r}, {g}, {b}"
        except (ValueError, IndexError):
            print(f"Warning: Invalid hex color '{hex_color}'. Returning None.")
            return None
    app.jinja_env.filters['hex_to_rgb'] = hex_to_rgb

    def format_activity_timestamp(ms_timestamp):
        if ms_timestamp:
            try:
                dt_obj = datetime.fromtimestamp(ms_timestamp / 1000, tz=timezone.utc)
                ist_offset = timedelta(hours=5, minutes=30)
                ist_tz = timezone(ist_offset)
                ist_dt_obj = dt_obj.astimezone(ist_tz)
                return ist_dt_obj.strftime('%Y-%m-%d %H:%M:%S IST')
            except (ValueError, TypeError):
                pass
        return 'N/A'
    app.jinja_env.filters['format_activity_timestamp'] = format_activity_timestamp
    app.jinja_env.filters['format_timestamp_relative'] = format_timestamp_relative
    # --- End Jinja Custom Filters ---

    app.jinja_env.globals['weekday_labels'] = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

    def parse_dashboard_filters(args):
        interval = (args.get('interval') or 'lifetime').strip().lower()
        if interval not in {'daily', 'weekly', 'monthly', 'lifetime'}:
            interval = 'lifetime'

        today = datetime.now(timezone.utc).date()
        standard_filters = build_standard_filter_definitions(today)
        filters = dict(standard_filters.get(interval, standard_filters['lifetime']))
        filters['preset_key'] = interval
        return filters

    async def build_filtered_dashboard_sections(base_data, filters):
        preset_key = filters.get('preset_key')
        precomputed_filters = base_data.get('precomputed_filters', {})
        if preset_key and preset_key in precomputed_filters:
            return deepcopy(precomputed_filters[preset_key])

        cache_key = (filters['interval'], filters['start_iso'], filters['end_iso'])
        now_ts = datetime.now(timezone.utc).timestamp()
        cached = dashboard_filter_cache.get(cache_key)
        if cached and (now_ts - cached['timestamp']) < FILTER_CACHE_TTL_SECONDS:
            return deepcopy(cached['data'])

        start_dt = datetime.combine(filters['start'], datetime.min.time(), tzinfo=timezone.utc)
        end_dt = datetime.combine(filters['end'] + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)

        chat_projection = {
            "timestamp": 1,
            "channel": 1,
            "user_id": 1,
            "author": 1,
            "content": 1,
            "emojis": 1,
            "_id": 0
        }
        member_projection = {
            "id": 1,
            "name": 1,
            "display_name": 1,
            "avatar": 1,
            "_id": 0
        }
        emoji_projection = {
            "name": 1,
            "url": 1,
            "id": 1,
            "animated": 1,
            "_id": 0
        }

        chat_logs = await db["chat_logs"].find({
            "timestamp": {
                "$gte": start_dt.isoformat(),
                "$lt": end_dt.isoformat()
            }
        }, chat_projection).to_list(length=None)
        emojis_json = await db["emojis"].find({}, emoji_projection).to_list(length=None)
        members_list = await db["members"].find({}, member_projection).to_list(length=None)

        filtered_stats = process_data(
            chat_logs,
            base_data.get('server', {}),
            [],
            emojis_json,
            members_list
        )
        filtered_stats = finalize_filtered_dashboard_stats(filtered_stats, filters)
        dashboard_filter_cache[cache_key] = {
            'timestamp': now_ts,
            'data': deepcopy(filtered_stats)
        }
        return filtered_stats

    async def get_dashboard_view_data(filters, include_filtered=True):
        base_data = await db["dashboard_analytics"].find_one({"_id": "live_stats"})
        if not base_data:
            return None

        base_data.pop('_id', None)
        base_data['processed_at_text'] = format_timestamp_relative(base_data.get('last_processed_at'))
        base_data['filter_state'] = {
            'interval': filters['interval'],
            'start': filters['start_iso'],
            'end': filters['end_iso'],
            'label': filters['label']
        }
        if include_filtered:
            filtered_stats = await build_filtered_dashboard_sections(base_data, filters)
            for key in ['messages', 'activity', 'top_users_data', 'heatmap', 'sentiment_data', 'channel_activity', 'active_streaks_data']:
                base_data[key] = filtered_stats.get(key, base_data.get(key))
            base_data['filtered_window'] = filtered_stats.get('filtered_window', {'messages': 0, 'top_members_count': 0, 'streak_members_count': 0})
            base_data['activity_insights'] = filtered_stats.get('activity_insights', base_data.get('activity_insights', {}))
            base_data['filter_state'] = filtered_stats.get('filter_state', base_data['filter_state'])
        else:
            preset_key = filters.get('preset_key')
            precomputed_filters = base_data.get('precomputed_filters', {})
            if preset_key and preset_key in precomputed_filters:
                filtered_stats = deepcopy(precomputed_filters[preset_key])
                for key in ['messages', 'activity', 'top_users_data', 'heatmap', 'sentiment_data', 'channel_activity', 'active_streaks_data']:
                    base_data[key] = filtered_stats.get(key, base_data.get(key))
                base_data['filtered_window'] = filtered_stats.get('filtered_window', {'messages': 0, 'top_members_count': 0, 'streak_members_count': 0})
                base_data['activity_insights'] = filtered_stats.get('activity_insights', base_data.get('activity_insights', {}))
                base_data['filter_state'] = filtered_stats.get('filter_state', base_data['filter_state'])
            else:
                base_data['filtered_window'] = {
                    'messages': int(base_data.get('messages', 0)),
                    'top_members_count': len(base_data.get('top_users_data', [])),
                    'streak_members_count': len(base_data.get('active_streaks_data', [])),
                }
        return base_data

    def is_allowed_dashboard_request(referer):
        allowed_origins = [
            'https://rainyseason.vercel.app/',
            'https://rainyseason.vercel.app/dashboard',
            'https://rainyseason.vercel.app/dashboard-original',
        ]
        if referer and (
            referer in allowed_origins or
            referer.startswith('https://rainyseason.vercel.app/users/') or
            referer.startswith('https://rainyseason.vercel.app/dashboard?') or
            referer.startswith('https://rainyseason.vercel.app/dashboard-original?')
        ):
            return True

        if not referer:
            return False

        parsed = urlparse(referer)
        if parsed.netloc in {'127.0.0.1:24594', 'localhost:24594'}:
            return parsed.path in {'/', '/dashboard', '/dashboard-original'} or parsed.path.startswith('/users/')
        return False
    
    # --- REFACTORED load_data TO USE MONGODB ---
    async def load_data(*requested):
        chat_logs = []
        server_info = {"name": "Server", "icon": "default.png", "total_users": 0, "active_users": 0, "updated_at": "N/A"}
        boosters = []
        emojis_json = []
        members_list = []

        load_all = not requested or "all" in requested

        try:
            if load_all or "chat_logs" in requested:
                chat_logs = await db["chat_logs"].find({}).to_list(length=None)
        except Exception as e:
            print(f"Error loading chat_logs from MongoDB: {e}")

        try:
            if load_all or "server_info" in requested:
                # Use the fixed ID we set in the bot script
                server_info_data = await db["server_info"].find_one({"_id": "server_config"})
                if server_info_data:
                    server_info = server_info_data
        except Exception as e:
            print(f"Error loading server_info from MongoDB: {e}")

        try:
            if load_all or "boosters" in requested:
                boosters = await db["boosters"].find({}).to_list(length=None)
        except Exception as e:
            print(f"Error loading boosters from MongoDB: {e}")

        try:
            if load_all or "emojis" in requested:
                emojis_json = await db["emojis"].find({}).to_list(length=None)
        except Exception as e:
            print(f"Error loading emojis from MongoDB: {e}")

        try:
            if load_all or "members" in requested:
                members_list = await db["members"].find({}).to_list(length=None)
        except Exception as e:
            print(f"Error loading members from MongoDB: {e}")

        # CRITICAL: Remove the MongoDB '_id' field before sending to templates/pandas
        for doc in chat_logs: doc.pop('_id', None)
        server_info.pop('_id', None) # Pop the fixed ID
        for doc in boosters: doc.pop('_id', None)
        for doc in emojis_json: doc.pop('_id', None)
        for doc in members_list: doc.pop('_id', None)

        return chat_logs, server_info, boosters, emojis_json, members_list

    async def load_server_info_only():
        """A tiny, fast function to get just the server info."""
        try:
            server_info_data = await db["server_info"].find_one({"_id": "server_config"})
            if server_info_data:
                server_info_data.pop('_id', None)
                return server_info_data
        except Exception as e:
            print(f"Error loading server_info: {e}")
        # Fallback in case of error
        return {"name": "Server", "icon": "default.png", "total_users": 0, "active_users": 0, "updated_at": "N/A"}

    # --- Routes are UNCHANGED, they just use the new load_data ---
    @app.route('/robots.txt')
    async def robots_txt():
        body = "User-agent: *\nAllow: /\nSitemap: https://rainyseason.vercel.app/sitemap.xml\n"
        return Response(body, content_type='text/plain')

    @app.route('/sitemap.xml')
    async def sitemap_xml():
        body = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://rainyseason.vercel.app/</loc>
  </url>
  <url>
    <loc>https://rainyseason.vercel.app/privacypolicy</loc>
  </url>
  <url>
    <loc>https://rainyseason.vercel.app/termsandservice</loc>
  </url>
</urlset>
"""
        return Response(body, content_type='application/xml')
    @app.route('/')
    async def index():
        print("Loading server info for homepage...")
        server_info = await load_server_info_only() # Optimized
        current_year = datetime.now().year
        html = await render_template('index.html', server=server_info, current_year=current_year)
        return Response(await clean_html(html), content_type='text/html')

    @app.route('/dashboard')
    async def dashboard():
        filters = parse_dashboard_filters(request.args)
        data = await get_dashboard_view_data(filters, include_filtered=False)
        if not data:
            print("Analytics data not found. Bot may still be processing.")
            return jsonify({"error": "Data is processing"}), 503
        html = await render_template('dashboard.html', **data)
        return Response(await clean_html(html), content_type='text/html')

    @app.route('/dashboard-original')
    async def dashboard_original():
        filters = parse_dashboard_filters(request.args)
        data = await get_dashboard_view_data(filters, include_filtered=False)
        if not data:
            print("Analytics data not found. Bot may still be processing.")
            return jsonify({"error": "Data is processing"}), 503
        html = await render_template('dashboard_original.html', **data)
        return Response(await clean_html(html), content_type='text/html')
    
    @app.route('/privacypolicy')
    async def privacy_policy():
        server_info = await load_server_info_only() # Optimized
        print("Loading privacy policy...")
        html = await render_template('privacypolicy.html', server=server_info)
        return Response(await clean_html(html), content_type='text/html')
    
    @app.route('/termsandservice')
    async def terms_and_service():
        server_info = await load_server_info_only() # Optimized
        print("Loading terms and conditions...")
        html = await render_template('termsandconditions.html', server=server_info)
        return Response(await clean_html(html), content_type='text/html')
    
    @app.route('/users/<user_id>')
    async def user_profile(user_id):
        # --- OPTIMIZATION ---
        # We now load server_info, ONE member, and ONE user's chats
        
        # 1. Get server info (fast)
        server_info = await load_server_info_only()

        # 2. Get ONLY the one member we care about (fast)
        current_member_detail = await db["members"].find_one({"id": user_id})
        
        if not current_member_detail:
            return await page_not_found(404) 
        
        # 3. Get ONLY this user's chat logs (fast)
        user_chat_logs = await db["chat_logs"].find({"user_id": user_id}).to_list(length=None)
        for doc in user_chat_logs: doc.pop('_id', None)
        
        # --- END OPTIMIZATION ---

        base_user_info = {'name': current_member_detail.get('name', 'N/A'), 'avatar': current_member_detail.get('avatar', 'default.png')}
        
        user_data = {
            'id': user_id,
            'name': base_user_info.get('name', 'N/A'),
            'avatar': base_user_info.get('avatar', 'default.png'),
            'total_messages': 0, 'first_message_date': 'N/A', 'last_message_date': 'N/A',
            'last_message_iso': None, 'channels_active_in': 0, 'top_channels': {},
            'activity': [{'day': i, 'messages': 0} for i in range(7)],
            'global_name': current_member_detail.get('global_name', None), 
            'display_name': current_member_detail.get('display_name', base_user_info.get('name', 'N/A')),
            'accent_color': current_member_detail.get('accent_color', None), 
            'banner': current_member_detail.get('banner', None),
            'joined_at': current_member_detail.get('joined_at', None), 
            'account_created_at': current_member_detail.get('account_created_at', None), 
            'is_owner': current_member_detail.get('is_owner', False),
            'is_admin': current_member_detail.get('is_admin', False),
            'is_mod': current_member_detail.get('is_mod', False),
            'is_rain_keeper': current_member_detail.get('is_rain_keeper', False),
            'level_role': current_member_detail.get('level_role', None),
            'gender_pronouns': current_member_detail.get('gender_pronouns', None),
            'roles': current_member_detail.get('roles', []),
            'is_booster': current_member_detail.get('is_booster', False),
            'boosted_since': current_member_detail.get('boosted_since', None), 
            'timed_out_until': current_member_detail.get('timed_out_until', None), 
            'status': current_member_detail.get('status', 'offline'),
            'desktop_status': current_member_detail.get('desktop_status', 'unknown'),
            'mobile_status': current_member_detail.get('mobile_status', 'unknown'),
            'web_status': current_member_detail.get('web_status', 'unknown'),
            'activities': current_member_detail.get('activities', []), 
            'public_flags': current_member_detail.get('public_flags', 0), 
            'guild_permissions_value': current_member_detail.get('guild_permissions_value', 0), 
            'top_role_name': current_member_detail.get('top_role_name', None),
        }
        
        user_data['joined_at_formatted'] = format_timestamp_relative(user_data['joined_at'])
        user_data['account_created_at_formatted'] = format_timestamp_relative(user_data['account_created_at'])
        user_data['timed_out_until_formatted'] = format_timestamp_relative(user_data['timed_out_until'])
        user_data['last_message_formatted'] = 'N/A'
        if user_data['is_booster'] and user_data['boosted_since']:
            user_data['boosted_since_formatted'] = format_timestamp_relative(user_data['boosted_since'])
        else:
            user_data['boosted_since_formatted'] = 'N/A'
        
        # This DataFrame is now tiny (only this user's messages)
        chat_df = pd.DataFrame(user_chat_logs) 

        if not chat_df.empty:
            chat_df['timestamp'] = pd.to_datetime(chat_df['timestamp'], errors='coerce', utc=True) 
            chat_df.dropna(subset=['timestamp'], inplace=True)
            
            if not chat_df.empty: # Re-check after potential dropna
                chat_df['day'] = chat_df['timestamp'].dt.dayofweek
                activity_by_day = chat_df.groupby('day').size().reindex(range(7), fill_value=0)
                user_activity_list = [{'day': day, 'messages': messages} for day, messages in activity_by_day.items()]
                last_dt = chat_df['timestamp'].max() if not chat_df.empty else None
                user_data.update({
                    'total_messages': len(chat_df),
                    'first_message_date': chat_df['timestamp'].min().strftime('%Y-%m-%d %H:%M:%S UTC'), 
                    'last_message_iso': last_dt.isoformat() if last_dt is not None else None, 
                    'channels_active_in': chat_df['channel'].nunique(),
                    'top_channels': chat_df['channel'].value_counts().head(5).to_dict(),
                    'activity': user_activity_list,
                    'last_message_formatted': format_timestamp_relative(last_dt.isoformat()) if last_dt else 'N/A'
                })

        html = await render_template('user_profile.html', user=user_data, server=server_info)
        return Response(await clean_html(html), content_type='text/html')


    @app.route('/invite')
    async def invite():
        server_info = await load_server_info_only() # Optimized
        print(server_info["invite_code"])
        invite_link="https://discord.gg/" + server_info['invite_code']
        return redirect(invite_link)
    
    @app.route('/.well-known/discord')
    async def discord():
        content="dh=592135e2b0698d4aaa669dd5ec2dceee025e3a8b"
        return Response(content, content_type='text/plain')
    
    @app.route('/api/data')
    async def api_data():
        referer = request.headers.get('Referer')
        if is_allowed_dashboard_request(referer):
            filters = parse_dashboard_filters(request.args)
            data = await get_dashboard_view_data(filters)
            if not data:
                return jsonify({"error": "Data is processing"}), 503
            return jsonify(data)
        else:
            print(f"Forbidden access from Referer: {referer}")
            return abort(403)
        
    
    # --- HEAVILY OPTIMIZED /api/members/search ---
    @app.route('/api/members/search')
    async def search_members():
        referer = request.headers.get('Referer')
        if is_allowed_dashboard_request(referer):
            query = request.args.get('q', '').strip()
            if not query:
                return jsonify([]) 

            # This is MUCH faster than load_data()
            # It performs the search inside the database
            
            # Create a case-insensitive regex query
            # We add \b for "word boundary" to match starts of names
            regex_query = re.compile(f"\\b{re.escape(query)}.*", re.IGNORECASE)

            # Search across multiple relevant fields
            search_filter = {
                "$or": [
                    {"id": query}, # Exact ID match
                    {"name": regex_query},
                    {"global_name": regex_query},
                    {"display_name": regex_query}
                ]
            }
            
            # Project only the fields we need for the search result
            projection = {
                "id": 1,
                "name": 1,
                "global_name": 1,
                "display_name": 1,
                "avatar": 1,
                "_id": 0  # Exclude the MongoDB _id
            }

            cursor = db["members"].find(search_filter, projection).limit(10)
            final_results = await cursor.to_list(length=10)
            
            return jsonify(final_results)
        
        else:
            print(f"Forbidden access from Referer: {referer}")
            return abort(403)

    # --- Error Handlers are UNCHANGED ---
    @app.errorhandler(403)
    async def forbidden_page(e):
        server_info = await load_server_info_only() # Optimized
        icon = server_info.get('icon')
        banner = server_info.get('banner', '/static/banner.gif') 
        return await render_template('403.html', icon=icon, banner=banner), 403

    @app.errorhandler(404)
    async def page_not_found(e):
        server_info = await load_server_info_only() # Optimized
        icon = server_info.get('icon')
        banner = server_info.get('banner')
        return await render_template('404.html', icon=icon, banner=banner), 404
    
    return app
