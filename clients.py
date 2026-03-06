#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Client initialisation for Pinecone, OpenAI, and Redis.

Credentials are loaded on-demand via SecureCredentialManager:
- Not stored in memory longer than necessary
- Fresh fetch from environment each time
- Sanitized error messages to prevent credential exposure
"""

import logging
import os
from typing import Optional

from openai import OpenAI
from pinecone import Pinecone
from redis import Redis

from alfred_exceptions import ConfigError
from credential_manager import SecureCredentialManager
from log_sanitiser import sanitise_message

# print("pinecone loaded from:", Pinecone.__module__)
# print("openai loaded from:", OpenAI.__module__)
# print("redis loaded from:", Redis.__module__)


class ClientManager:
    """Manages lazy-loaded clients for Pinecone, OpenAI, and Redis."""

    _pc: Optional[Pinecone] = None
    _oai: Optional[OpenAI] = None
    _redis: Optional[Redis] = None

    @classmethod
    def get_pc(cls) -> Pinecone:
        """
        Lazy-load Pinecone client.
        Only creates client when first needed.
        Credentials fetched fresh from environment each time.
        """
        if cls._pc is not None:
            return cls._pc

        try:
            # Get credential fresh from environment (not cached in memory)
            api_key = SecureCredentialManager.get_pinecone_api_key()
            cls._pc = Pinecone(api_key=api_key)
            return cls._pc
        except KeyError as e:
            logging.error("Pinecone API key not configured")
            raise ConfigError(
                "PINECONE_API_KEY not set. Please configure credentials."
            ) from e
        except Exception as e:
            # Log sanitized error - credential manager has already redacted the key
            logging.error("Failed to initialise Pinecone: %s", sanitise_message(str(e)))
            raise ConfigError("Failed to initialise Pinecone client") from e

    @classmethod
    def get_oai(cls) -> OpenAI:
        """
        Lazy-load OpenAI client.
        Credentials fetched fresh from environment each time.
        """
        if cls._oai is not None:
            return cls._oai

        try:
            # Get credential fresh from environment (not cached in memory)
            api_key = SecureCredentialManager.get_openai_api_key()
            cls._oai = OpenAI(api_key=api_key)
            return cls._oai
        except KeyError as e:
            logging.error("OpenAI API key not configured")
            raise ConfigError(
                "OPENAI_API_KEY not set. Please configure credentials."
            ) from e
        except Exception as e:
            # Log sanitized error - credential manager has already redacted the key
            logging.error("Failed to initialise OpenAI: %s", sanitise_message(str(e)))
            raise ConfigError("Failed to initialise OpenAI client") from e

    @classmethod
    def get_redis(cls) -> Redis:
        """
        Lazy-load Redis client.
        Only creates client when first needed.
        """
        if cls._redis is not None:
            return cls._redis

        redis_host = os.environ.get("REDIS_HOST")
        redis_port_str = os.environ.get("REDIS_PORT", "0")
        redis_username = os.environ.get("REDIS_USERNAME", "")
        redis_password = os.environ.get("REDIS_PASSWORD", "")

        if not redis_host:
            raise ConfigError("REDIS_HOST is not set in environment")

        try:
            redis_port = int(redis_port_str)
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"Invalid REDIS_PORT: {redis_port_str}") from exc

        if redis_port <= 0 or redis_port > 65535:
            raise ConfigError("REDIS_PORT must be between 1 and 65535")

        try:
            cls._redis = Redis(
                host=redis_host,
                port=redis_port,
                decode_responses=True,
                username=redis_username if redis_username else None,
                password=redis_password if redis_password else None,
                health_check_interval=30,
            )
            return cls._redis
        except Exception as e:
            # Log sanitized error
            logging.error("Failed to initialise Redis: %s", sanitise_message(str(e)))
            raise ConfigError("Failed to initialise Redis client") from e


def get_pc() -> Pinecone:
    """Lazy-load Pinecone client."""
    return ClientManager.get_pc()


def get_oai() -> OpenAI:
    """Lazy-load OpenAI client."""
    return ClientManager.get_oai()


def get_redis() -> Redis:
    """Lazy-load Redis client."""
    return ClientManager.get_redis()
