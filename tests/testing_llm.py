from hermes.app.cli import run_cli
from hermes.services.conversation_service import ConversationService
from hermes.adapters.local_openai_compatible.llm_engine import LocalOpenAICompatibleLLM

local_llm = LocalOpenAICompatibleLLM(base_url="http://127.0.0.1:8080", model="qwen_34b", timeout_s=120)
ConvServ = ConversationService(llm=local_llm)
run_cli(ConvServ)