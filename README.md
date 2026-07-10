<h1 align="center">RainySeason</h1>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white">
  <img src="https://img.shields.io/badge/discord.py-2.5+-5865F2?style=for-the-badge&logo=discord&logoColor=white">
  <img src="https://img.shields.io/badge/Quart-Async_Framework-1C1C1C?style=for-the-badge">
  <img src="https://img.shields.io/badge/MongoDB-Database-47A248?style=for-the-badge&logo=mongodb&logoColor=white">
  <img src="https://img.shields.io/badge/Groq-AI-F55036?style=for-the-badge">
  <img src="https://img.shields.io/github/last-commit/Sayak-Saha/RainySeason?style=for-the-badge">
</p>

An AI-powered Discord community platform featuring **Rainy AI**, moderation tools, guild discovery, and an integrated analytics dashboard. Built with Python, Quart, MongoDB, and the Discord API for the **Rainy Season** Discord community.

> 🌐 **Live Dashboard:** https://rainyseason.vercel.app/  
> 💬 **Production Server:** https://discord.com/invite/CvU77YA65K

---

## 🤖 Rainy AI

Rainy AI is the intelligent assistant that powers the Rainy Season community.

### Features

- Multi-model AI with automatic model fallback
- Context-aware conversations
- Optional live web search
- Emoji-aware responses
- AI status and rate-limit monitoring
- Automatic model preference learning

<p align="center">
  <img src="screenshots/ai-chat.png" width="900">
</p>

---

## 📊 Community Analytics Dashboard

A responsive web dashboard providing insights into server activity and member engagement.

### Features

- Live server overview
- Member activity analytics
- Interactive charts and statistics
- Searchable member profiles
- Activity trends
- Sentiment analytics
- Responsive dashboard interface

<p align="center">
  <img src="screenshots/dashboard.png" width="900">
</p>

---

## 🏷️ Guild Tag Management

A scalable discovery system for Discord's discoverable communities.

### Features

- Search guilds by discoverable tag
- Register guilds using Discord invite links
- Automatic invite validation
- Automatic guild metadata synchronization
- Removes invalid or expired guild entries
- MongoDB-powered indexed searches
- Supports **27,000+ indexed Discord guilds**

<p align="center">
  <img src="screenshots/guild-tags.png" width="900">
</p>

---

## 🛡️ Moderation

Automated moderation utilities designed to keep communities organized and reduce spam.

### Features

- Cross-channel duplicate spam detection
- Automatic member timeouts
- Duplicate message cleanup
- Direct message notifications
- Webhook moderation logging

---

## 🎉 Lucky Member System

Automatically rewards active community members while maintaining a synchronized Hall of Fame.

### Features

- Random Lucky Member selection
- Automatic role assignment
- Hall of Fame tracking
- Automatic synchronization
- Permission showcase

<p align="center">
  <img src="screenshots/lucky-member.png" width="900">
</p>

---

## 🛠️ Technologies Used

- Python
- discord.py
- Quart
- MongoDB
- Groq API
- Discord API
- HTML
- CSS
- JavaScript
- Chart.js
- asyncio

---

## 📂 Project Structure

```text
RainySeason
├── assets/         Project assets
├── functions/      Core bot functionality
├── rainyai/        Rainy AI modules and Discord interactions
├── static/         Website assets
├── templates/      Dashboard templates
└── ...
```

---

## 📌 About

RainySeason is my primary long-term software project, developed for the **Rainy Season** Discord community.

The platform combines **Rainy AI**, moderation, guild discovery, automated community systems, and a web-based analytics dashboard into a unified ecosystem for community management. It continues to evolve with new AI capabilities, automation features, and analytics tools.

---

## 📄 License

This project is licensed under the MIT License.
