from dataclasses import dataclass
from typing import Any, Callable, cast

from pydantic import BaseModel
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts.chat import ChatPromptTemplate, ChatPromptValue
from langgraph.utils.runnable import RunnableCallable

TokenCounter = Callable[[list[BaseMessage]], int]


DEFAULT_INITIAL_SUMMARY_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("placeholder", "{messages}"),
        ("user", "Create a summary of the conversation above:"),
    ]
)


DEFAULT_EXISTING_SUMMARY_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("placeholder", "{messages}"),
        (
            "user",
            "This is summary of the conversation to date: {existing_summary}\n\n"
            "Extend the summary by taking into account the new messages above:",
        ),
    ]
)

DEFAULT_FINAL_SUMMARY_PROMPT = ChatPromptTemplate.from_messages(
    [
        # if exists
        ("placeholder", "{system_message}"),
        ("system", "Summary of conversation earlier: {summary}"),
        ("placeholder", "{messages}"),
    ]
)


@dataclass
class SummaryInfo:
    # the summary of the conversation so far
    summary: str
    # the IDs of the messages that have been most recently summarized
    summarized_message_ids: list[str]
    # keep track of the total number of messages that have been summarized thus far
    total_summarized_messages: int = 0


@dataclass
class SummarizationResult:
    # the messages that will be returned to the user
    messages: list[BaseMessage]
    # SummaryInfo (empty if messages were not summarized)
    summary_info: SummaryInfo | None = None


def summarize_messages(
    messages: list[BaseMessage],
    *,
    existing_summary_info: SummaryInfo | None,
    model: BaseChatModel,
    max_tokens: int,
    max_summary_tokens: int = 1,
    token_counter: TokenCounter = len,
    initial_summary_prompt: ChatPromptTemplate = DEFAULT_INITIAL_SUMMARY_PROMPT,
    existing_summary_prompt: ChatPromptTemplate = DEFAULT_EXISTING_SUMMARY_PROMPT,
    final_prompt: ChatPromptTemplate = DEFAULT_FINAL_SUMMARY_PROMPT,
) -> SummarizationResult:
    """Summarize messages when they exceed a token limit and replace them with a single summary message.

    The function processes the messages from oldest to newest: once the cumulative number of message tokens
    reaches max_tokens, all messages within the token limit are summarized and replaced with a new summary message.
    The resulting list of messages is [summary_message] + remaining_messages.

    Args:
        messages: The list of messages to process.
        existing_summary_info: Optional existing summary info.
            If provided, will be used in the following ways:
            - only the messages since the last summary will be processed
            - if no new summary is generated, the existing summary will be applied to the returned messages
            - if a new summary needs to be generated, 
        model: The language model to use for generating summaries.
        max_tokens: Maximum number of tokens to return.
            Will be used as a threshold for triggering the summarization: once the cumulative number of message tokens,
            all messages within max_tokens will be summarized.
        max_summary_tokens: Maximum number of tokens to return from the summarization LLM.
        token_counter: Function to count tokens in a message. Defaults to approximate counting.
        initial_summary_prompt: Prompt template for generating the first summary.
        existing_summary_prompt: Prompt template for updating an existing summary.
        final_prompt: Prompt template that combines summary with the remaining messages before returning.

    Returns:
        A SummarizationResult object containing the updated messages and summary.
            - messages: list of updated messages ready to be input to the LLM
            - summary_info: SummaryInfo object
                - summary: text of the latest summary
                - summarized_message_ids: list of message IDs that were most recently summarized
                - total_summarized_messages: running total of the number of messages that have been summarized thus far
    """
    if max_summary_tokens >= max_tokens:
        raise ValueError("`max_summary_tokens` must be less than `max_tokens`.")

    # First handle system message if present
    if messages and isinstance(messages[0], SystemMessage):
        existing_system_message = messages[0]
        # remove the system message from the list of messages to summarize
        messages = messages[1:]
        # adjust the token budget to account for the system message to be added
        max_tokens -= token_counter([existing_system_message])
    else:
        existing_system_message = None

    if not messages:
        return SummarizationResult(
            summary_info=existing_summary_info,
            messages=(
                messages
                if existing_system_message is None
                else [existing_system_message] + messages
            ),
        )

    summary_info = existing_summary_info
    total_summarized_messages = (
        summary_info.total_summarized_messages if summary_info else 0
    )

    # Single pass through messages to count tokens and find cutoff point
    n_tokens = 0
    idx = max(0, total_summarized_messages - 1)
    # we need to output messages that fit within max_tokens.
    # assuming that the summarization LLM also needs at most max_tokens
    # that will be turned into at most max_summary_tokens, you can try
    # to process at most max_tokens * 2 - max_summary_tokens
    max_total_tokens = max_tokens * 2 - max_summary_tokens
    for i in range(total_summarized_messages, len(messages)):
        n_tokens += token_counter([messages[i]])

        # If we're still under max_tokens, update the potential cutoff point
        if n_tokens <= max_tokens:
            idx = i

        # Check if we've exceeded the absolute maximum
        if n_tokens >= max_total_tokens:
            raise ValueError(
                f"summarize_messages cannot handle more than {max_total_tokens} tokens. "
                "Please increase the `max_tokens` or decrease the input size."
            )

    # If we haven't exceeded max_tokens, we don't need to summarize
    # Note: we don't return here since we might still need to include the existing summary
    if n_tokens <= max_tokens:
        messages_to_summarize = None
    else:
        messages_to_summarize = messages[total_summarized_messages : idx + 1]

    # If the last message is:
    # (1) an AI message with tool calls - remove it
    #   to avoid issues w/ the LLM provider (as it will lack a corresponding tool message)
    # (2) a human message - remove it,
    #   since it is a user input and it doesn't make sense to summarize it without a corresponding AI message
    while messages_to_summarize and (
        (
            isinstance(messages_to_summarize[-1], AIMessage)
            and messages_to_summarize[-1].tool_calls
        )
        or isinstance(messages_to_summarize[-1], HumanMessage)
    ):
        messages_to_summarize.pop()

    if messages_to_summarize:
        if existing_summary_info:
            summary_messages = cast(
                ChatPromptValue,
                existing_summary_prompt.invoke(
                    {
                        "messages": messages_to_summarize,
                        "existing_summary": summary_info.summary,
                    }
                ),
            )
        else:
            summary_messages = cast(
                ChatPromptValue,
                initial_summary_prompt.invoke({"messages": messages_to_summarize}),
            )

        summary_message_response = model.invoke(summary_messages.messages)
        total_summarized_messages += len(messages_to_summarize)
        summary_info = SummaryInfo(
            summary=summary_message_response.content,
            summarized_message_ids=[message.id for message in messages_to_summarize],
            total_summarized_messages=total_summarized_messages,
        )

    if summary_info:
        updated_messages = cast(
            ChatPromptValue,
            final_prompt.invoke(
                {
                    "system_message": [existing_system_message]
                    if existing_system_message
                    else [],
                    "summary": summary_info.summary,
                    "messages": messages[total_summarized_messages:],
                }
            ),
        )
        return SummarizationResult(
            summary_info=summary_info,
            messages=updated_messages.messages,
        )
    else:
        # no changes are needed
        return SummarizationResult(
            summary_info=None,
            messages=(
                messages
                if existing_system_message is None
                else [existing_system_message] + messages
            ),
        )


class SummarizationNode(RunnableCallable):
    def __init__(
        self,
        *,
        model: BaseChatModel,
        max_tokens: int,
        max_summary_tokens: int = 1,
        token_counter: TokenCounter = len,
        initial_summary_prompt: ChatPromptTemplate = DEFAULT_INITIAL_SUMMARY_PROMPT,
        existing_summary_prompt: ChatPromptTemplate = DEFAULT_EXISTING_SUMMARY_PROMPT,
        final_prompt: ChatPromptTemplate = DEFAULT_FINAL_SUMMARY_PROMPT,
        messages_key: str = "messages",
        output_messages_key: str = "messages",
        name: str = "summarization",
    ) -> None:
        super().__init__(self._func, name=name, trace=False)
        self.model = model
        self.max_tokens = max_tokens
        self.max_summary_tokens = max_summary_tokens
        self.token_counter = token_counter
        self.initial_summary_prompt = initial_summary_prompt
        self.existing_summary_prompt = existing_summary_prompt
        self.final_prompt = final_prompt
        self.messages_key = messages_key
        self.output_messages_key = output_messages_key

    def _func(self, input: dict[str, Any] | BaseModel) -> dict[str, Any]:
        if isinstance(input, dict):
            messages = input.get(self.messages_key)
            context = input.get("context", {})
        elif isinstance(input, BaseModel):
            messages = getattr(input, self.messages_key, None)
            context = getattr(input, "context", {})
        else:
            raise ValueError(f"Invalid input type: {type(input)}")

        if messages is None:
            raise ValueError("Missing required field `messages` in the input.")

        summarization_result = summarize_messages(
            messages,
            existing_summary_info=context.get("summary_info"),
            model=self.model,
            max_tokens=self.max_tokens,
            max_summary_tokens=self.max_summary_tokens,
            token_counter=self.token_counter,
            initial_summary_prompt=self.initial_summary_prompt,
            existing_summary_prompt=self.existing_summary_prompt,
            final_prompt=self.final_prompt,
        )

        state_update = {self.output_messages_key: summarization_result.messages}
        if summarization_result.summary_info:
            state_update["context"] = {
                **context,
                "summary_info": summarization_result.summary_info,
            }
        return state_update
