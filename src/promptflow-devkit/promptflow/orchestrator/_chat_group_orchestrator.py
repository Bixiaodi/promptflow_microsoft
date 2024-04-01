from typing import Optional, List, Mapping, Dict, Any
from promptflow._sdk.entities._chat_group._chat_role import ChatRole
from promptflow.batch._base_executor_proxy import AbstractExecutorProxy
from promptflow.executor._result import LineResult
from promptflow.storage import AbstractRunStorage
from promptflow.batch._batch_inputs_processor import BatchInputsProcessor
from promptflow._utils.execution_utils import apply_default_value_for_input
from promptflow._proxy._proxy_factory import ProxyFactory
from promptflow._utils.logger_utils import bulk_logger
from promptflow._constants import CONVERSATION_HISTORY_EXPRESSION, CONVERSATION_HISTORY_OUTPUT_KEY
from promptflow.orchestrator._errors import (
    InvalidChatRoleCount,
    MissingConversationHistoryExpression,
    MultipleConversationHistoryInputsMapping
)


class ChatGroupOrchestrator:
    def __init__(
        self,
        chat_group_roles: List[ChatRole],
        max_turn: Optional[int] = None,
        storage: Optional[AbstractRunStorage] = None,
        max_lines_count: Optional[int] = None,
        **kwargs
    ):
        """Chat group orchestrator schedule runs for each line in batch inputs.

        :param chat_group_roles: chat group roles
        :type chat_group_roles: List[ChatRole]
        :param max_turn: max turn of chat, defaults to None
        :type max_turn: Optional[int], optional
        :param storage: storage, defaults to None
        :type storage: Optional[AbstractRunStorage], optional
        :param max_lines_count: max lines from inputs, defaults to None
        :type max_lines_count: Optional[int], optional
        """
        self._storage = storage
        self._max_turn = max_turn
        self._chat_group_roles = chat_group_roles
        self._max_lines_count = max_lines_count

        if len(self._chat_group_roles) < 2:
            bulk_logger.error(f"Invalid chat group role count: {len(self._chat_group_roles)}")
            message = (
                f"Invalid chat group role count: {len(self._chat_group_roles)}. "
                "Please define 2 chat group roles at least."
            )
            raise InvalidChatRoleCount(message=message)

        self._executor_proxies: List[AbstractExecutorProxy] = self._create_executor_proxy(**kwargs)

    @classmethod
    def create(
        cls,
        chat_group_roles: List[ChatRole],
        max_turn: Optional[int] = None,
        storage: Optional[AbstractRunStorage] = None,
        max_lines_count: Optional[int] = None,
    ) -> "ChatGroupOrchestrator":

        return ChatGroupOrchestrator(chat_group_roles, max_turn, storage, max_lines_count)

    def _create_executor_proxy(self, **kwargs) -> List[AbstractExecutorProxy]:
        """create executor proxy for each chat role according to language

        :return: proxy list
        :rtype: List[AbstractExecutorProxy]
        """
        executor_proxy_list = []
        executor_proxy_factory = ProxyFactory()
        for chat_role in self._chat_group_roles:
            executor_proxy = executor_proxy_factory.create_executor_proxy(
                flow_file=chat_role.flow_file,
                working_dir=chat_role.working_dir,
                connections=chat_role.connections,
                storage=self._storage,
                language=chat_role.check_language_from_yaml(),
                **kwargs
            )
            bulk_logger.info(f"Created executor proxy for role:{chat_role.role}. name: {chat_role.name}")
            executor_proxy_list.append(executor_proxy)
        return executor_proxy_list

    async def destroy(self):
        for executor_proxy in self._executor_proxies:
            await executor_proxy.destroy()

    async def _schedule_line_runs(
            self,
            line_index: int,
            inputs: Mapping[str, Any] = None,
            run_id: str = None,
            ) -> LineResult:
        """schedule runs for each line in batch inputs.
        It also resolve flow inputs and flow outputs for each turn.

        :param line_index: line index in batch inputs
        :type line_index: int
        :param inputs: raw input line of line_index, defaults to None
        :type inputs: Mapping[str, Any], optional
        :param run_id: run id, defaults to None
        :type run_id: str, optional
        :return: line result
        :rtype: LineResult
        """
        outputs: dict = {}
        aggregation_inputs: dict = {}
        current_line_result: LineResult = None

        total_roles = len(self._chat_group_roles)
        conversation_history: List[Mapping[str, Any]] = []
        batch_inputs = self._process_batch_inputs(inputs)
        bulk_logger.info(f"Finish process batch inputs and applying inputs mapping for line number:{line_index}")

        bulk_logger.info(f"Start to schedule runs for run id: {run_id}, line number: {line_index}")

        for turn in range(self._max_turn):
            role_index = turn % total_roles
            executor_proxy = self._executor_proxies[role_index]
            chat_role = self._chat_group_roles[role_index]
            chat_role_input = batch_inputs[role_index]
            conversation_history_key = next(
                (key for key, value in chat_role.inputs_mapping.items()
                 if value == CONVERSATION_HISTORY_EXPRESSION), None
            )
            if conversation_history_key is None:
                bulk_logger.error(
                    f"Cannot find conversation expression mapping for "
                    f"chat role: {chat_role.role}. name: {chat_role.name}"
                )
                message = (
                    f"Cannot find conversation expression mapping for "
                    f"chat role: {chat_role.role}. name: {chat_role.name} "
                    f"Please use define {CONVERSATION_HISTORY_EXPRESSION} for a flow input."
                )
                raise MissingConversationHistoryExpression(message=message)
            chat_role_input[conversation_history_key] = conversation_history
            bulk_logger.info(
                f"Start to execute turn {turn}. role: {chat_role.role}. name: {chat_role.name}"
            )

            current_line_result = await executor_proxy.exec_line_async(chat_role_input, line_index, run_id)
            self._process_flow_outputs(
                turn,
                chat_role,
                current_line_result,
                conversation_history,
                outputs,
                aggregation_inputs)
            bulk_logger.info(
                f"Finish process line result for "
                f"line number: {line_index}, turn:{turn}. role:{chat_role.role}, name: {chat_role.name}"
            )

            if any(value == chat_role.stop_signal for value in current_line_result.output.values()):
                bulk_logger.info(
                    f"Stop chat since current turn align with stop signal. "
                    f"line number: {line_index}, turn:{turn}. role:{chat_role.role}, name: {chat_role.name}"
                )
                break

        bulk_logger.info(
            f"Finish schedule runs for run id: {run_id}, "
            f"line number: {line_index}, add conversation history to output"
        )
        outputs.update({CONVERSATION_HISTORY_OUTPUT_KEY: conversation_history})

        return LineResult(
            output=outputs,
            aggregation_inputs=aggregation_inputs,
            node_run_infos=current_line_result.node_run_infos,
            run_info=current_line_result.run_info
        )

    def _process_flow_outputs(
            self,
            index: int,
            chat_role: ChatRole,
            current_line_result: LineResult,
            conversation_history: List[Mapping[str, Any]],
            outputs: dict,
            aggregation_inputs: dict):

        current_turn = {"role": chat_role.role}
        current_turn.update(current_line_result.output)
        conversation_history.append(current_turn)

        outputs.update({index: current_turn})
        aggregation_inputs.update({index: current_line_result.aggregation_inputs})

    def _process_batch_inputs(self, inputs: Dict[str, Any]):
        batch_inputs: List = []
        for chat_role in self._chat_group_roles:
            if CONVERSATION_HISTORY_EXPRESSION not in chat_role.inputs_mapping.values():
                bulk_logger.error(
                    f"Cannot find conversation expression mapping for "
                    f"chat role: {chat_role.role}. name: {chat_role.name}"
                )
                message = (
                    f"Cannot find conversation expression mapping for "
                    f"chat role: {chat_role.role}. name: {chat_role.name} "
                    f"Please mapping {CONVERSATION_HISTORY_EXPRESSION} for a flow input."
                )
                raise MissingConversationHistoryExpression(message=message)

            conversation_mapping_count = list(chat_role.inputs_mapping.values()).count(CONVERSATION_HISTORY_EXPRESSION)
            if conversation_mapping_count > 1:
                bulk_logger.error(f"Multiple inputs mapping of {CONVERSATION_HISTORY_EXPRESSION}")
                message = (
                    f"chat role: {chat_role.role}. name: {chat_role.name} "
                    f"only accepts 1 inputs mapping for {CONVERSATION_HISTORY_EXPRESSION}"
                )
                raise MultipleConversationHistoryInputsMapping(message=message)

            batch_input_processor = BatchInputsProcessor(
                chat_role.working_dir,
                chat_role.flow.inputs,
                self._max_lines_count)
            batch_input = batch_input_processor._process_batch_inputs_line(inputs, chat_role.inputs_mapping)
            resolved_batch_input = apply_default_value_for_input(chat_role.flow.inputs, batch_input)

            batch_inputs.append(resolved_batch_input)

        return batch_inputs
