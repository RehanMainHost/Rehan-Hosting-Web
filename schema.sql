-- Supabase SQL editor e paste kore Run kor.
-- Panel nijei boot e CREATE TABLE IF NOT EXISTS chalaay, tai eita optional backup.
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS apps (
  id TEXT PRIMARY KEY, user_id TEXT, name TEXT, type TEXT, repo TEXT,
  service_id TEXT, url TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS bots (
  id TEXT PRIMARY KEY, user_id TEXT, name TEXT, token_enc TEXT, code TEXT,
  filename TEXT, webhook_secret TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS env_vars (
  id TEXT PRIMARY KEY, owner_type TEXT, owner_id TEXT, key TEXT, value_enc TEXT);
CREATE TABLE IF NOT EXISTS logs (
  id TEXT PRIMARY KEY, owner_type TEXT, owner_id TEXT, level TEXT, message TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS settings (
  id TEXT PRIMARY KEY, user_id TEXT, render_token_enc TEXT, render_owner_id TEXT);
