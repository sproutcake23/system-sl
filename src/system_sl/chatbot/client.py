"""Chat session management for System-SL."""

import json
from typing import Union, List

from system_sl.chatbot.system_adi import get_system_agent, reset_agent_cache
from system_sl.chatbot.config import ChatConfig, load_config, save_config

from langsmith import uuid7


class ChatSession:
    """Manages an active multi-turn conversation session with the System agent.

    Attributes:
        config (dict): The runtime configuration dictionary containing the `thread_id`
            for state tracking.
        agent (Runnable): The compiled LangGraph agent graph.
        model_name (str): The model name being used (for display purposes).
    """

    def __init__(
        self,
        thread_id: str | None = None,
        config: dict | None = None,
        model_name: str | None = None,
    ):
        """Initializes a new or existing ChatSession.

        Args:
            thread_id (str | None, optional): An existing unique identifier to resume a past conversation.
                If both thread_id and config are None, a new UUIDv7 is generated. Defaults to None.
            config (dict | None, optional): A complete pre-built LangChain/LangGraph
                configuration dictionary. Overrides thread_id if provided. Defaults to None.
            model_name (str | None, optional): Display name of the model being used.
        """
        if config:
            self.config = config
        else:
            chosen_id = thread_id or str(uuid7())
            self.config = {"configurable": {"thread_id": chosen_id}}

        # Select the pre-compiled agent (built on first use via lazy init)
        self.agent = get_system_agent()
        self.model_name = model_name or "system"

    def wrap_message(self, user_msg: Union[str, List[str]]) -> List[dict]:
        """Formats a raw string or list of strings into the standard message format.

        Args:
            user_msg (Union[str, List[str]]): A single text message string or a list
                of text message strings sent by the user.

        Returns:
            List[dict]: A list of formatted message dictionaries containing the 'role'
                and 'content' keys ready for LangGraph input.
        """
        if isinstance(user_msg, str):
            return [{"role": "user", "content": user_msg}]
        else:
            return [{"role": "user", "content": msg} for msg in user_msg]

    def run_turn(self, user_msg: Union[str, List[str]]) -> str:
        """Executes a single conversational turn with the agent.

        Args:
            user_msg (Union[str, List[str]]): The input text message(s) from the user.

        Returns:
            str: The extracted plain text content of the agent's reply.
        """
        formatted_messages = self.wrap_message(user_msg)

        response = self.agent.invoke(
            {"messages": formatted_messages},
            config=self.config
        )

        if response is not None and "messages" in response:
            last_message = response["messages"][-1]

            # Extract content property from the base message class instance
            if hasattr(last_message, 'content'):
                content = last_message.content

                # CASE 1: Content is a rich list: [{'type': 'text', 'text': '...'}]
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    return "".join(text_parts)  # Merges all text blocks into one clean string

                # CASE 2: Content is already a standard direct string
                if isinstance(content, str):
                    return content

            # CASE 3: Fallback check if the message structure parsed as a raw dictionary primitive
            elif isinstance(last_message, dict) and 'content' in last_message:
                content = last_message['content']
                if isinstance(content, list) and len(content) > 0:
                    return str(content[0].get('text', ''))
                return str(content)

        return "[The System failed to reply]"

    @classmethod
    def create_with_model(cls, model_name: str, thread_id: str | None = None) -> "ChatSession":
        """Create a session with a specific model by temporarily overriding config.

        Note: This creates a new agent with the specified model. The model change
        persists for the session lifetime.
        """
        # We'll handle model switching at the ChatPanel level by recreating sessions
        return cls(thread_id=thread_id, model_name=model_name)


def reload_agent() -> None:
    """Reload the agent with current config. Call after config changes."""
    reset_agent_cache()


if __name__ == "__main__":
    session = ChatSession()
    print(session.run_turn("Hello"))