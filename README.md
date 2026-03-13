# Breadbot — One-Click Deploy

Breadbot is an automated crypto trading dashboard that scans Solana and Base
for high-scoring token opportunities, monitors stablecoin yields across six
platforms, and gives you full control from a browser dashboard and Telegram.

## What This Deploys

This repository contains the deployment configuration for the Breadbot dashboard.
Clicking the button below provisions a fresh instance on Railway with the demo
database pre-loaded. Your license key (received after purchase at breadbot.app)
unlocks the full live trading features.

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template?template=https://github.com/breadbot-app/breadbot-deploy)

## Setup After Deploy

1. Open your Railway dashboard and find the deployed Breadbot service
2. Go to Variables and add your environment variables (full list in the
   setup guide included with your purchase)
3. Redeploy — the dashboard will be live at your Railway-provided URL
4. Follow the PDF setup guide to connect your exchange API keys and Telegram

## Requirements

You need a Railway account (free tier works for the dashboard). Live trading
features require Coinbase Advanced Trade and/or Kraken API keys, which you
configure as environment variables — never hardcoded.

## Support

Purchase support: hello@breadbot.app
Documentation: included with purchase (PDF setup guide)
