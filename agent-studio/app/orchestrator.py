from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

from .docker_runtime import DockerRuntime
from .event_bus import EventBus
from .store import EventStore

SUPERVISOR_AGENT_ID = 'senda-supervisor'
LEGACY_ORCHESTRATOR_AGENT_ID = 'senda-orchestrator'
# Backwards-compatible import/name for existing integrations.
ORCHESTRATOR_AGENT_ID = SUPERVISOR_AGENT_ID


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(value: Any, max_chars: int = 12000) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + '\n...[truncated]'
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) <= max_chars:
        return value
    return {'truncated': True, 'preview': text[:max_chars]}


def _json_from_text(text: str) -> dict[str, Any]:
    text = (text or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.I)
        text = re.sub(r'\s*```$', '', text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        data = json.loads(text[start:end + 1])
        if isinstance(data, dict):
            return data
    raise ValueError('LLM response did not contain a JSON object')


def _agent_catalog(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            'agent_id': a.get('agent_id'),
            'description': a.get('description', ''),
            'capabilities': a.get('capabilities', []),
            'tags': a.get('tags', []),
            'input_schema': a.get('input_schema', {}),
            'output_schema': a.get('output_schema', {}),
            'risk_level': a.get('risk_level', 'low'),
            'requires_approval': bool(a.get('requires_approval')),
        }
        for a in agents
    ]


class SupervisorPlanner:
    """Control-plane supervisor with interchangeable decision backends.

    Modes:
      - deterministic: offline development/smoke-test routing
      - llm: OpenAI-compatible chat/completions supervisor
      - jev: TypeSafe Jev/System One Choice routing

    The supervisor is not a worker Runtime. It receives the Goal, Agent Registry
    metadata and workflow history, then selects the next worker Agent or finish.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = dict(config or {})
        self.config = cfg
        self.mode = str(cfg.get('mode') or os.getenv('SENDA_STUDIO_ORCHESTRATOR_MODE', 'llm')).lower()
        self.base_url = str(cfg.get('base_url') or os.getenv('SENDA_STUDIO_LLM_BASE_URL', '')).rstrip('/')
        self.model = str(cfg.get('model') or os.getenv('SENDA_STUDIO_LLM_MODEL', ''))
        self.api_key = str(cfg.get('api_key') or os.getenv('SENDA_STUDIO_LLM_API_KEY', ''))
        self.timeout = float(cfg.get('timeout') or os.getenv('SENDA_STUDIO_LLM_TIMEOUT', '60'))

        self.jev_base_url = str(cfg.get('jev_base_url') or os.getenv('TYPESAFE_BASE_URL', '')).rstrip('/')
        self.jev_model = str(cfg.get('jev_model') or os.getenv('TYPESAFE_DEFAULT_MODEL', 'jev-latest'))
        self.jev_api_key = str(cfg.get('jev_api_key') or os.getenv('TYPESAFE_API_KEY', ''))
        self.jev_timeout = float(cfg.get('jev_timeout') or os.getenv('SENDA_STUDIO_JEV_TIMEOUT', '10'))
        self.jev_confidence_threshold = float(cfg.get('jev_confidence_threshold') or os.getenv('SENDA_STUDIO_JEV_CONFIDENCE_THRESHOLD', '0'))
        self.jev_fallback = str(cfg.get('jev_fallback') or os.getenv('SENDA_STUDIO_JEV_FALLBACK', 'none')).lower()

    def _deterministic(self, goal: str, agents: list[dict[str, Any]], history: list[dict[str, Any]]) -> dict[str, Any]:
        used = {str(s.get('agent_id')) for s in history if s.get('agent_id')}
        goal_tokens = set(re.findall(r'[A-Za-z0-9_.-]+', goal.lower()))
        best: tuple[int, dict[str, Any]] | None = None
        for agent in agents:
            aid = str(agent.get('agent_id') or agent.get('name') or '')
            if not aid or aid in used:
                continue
            haystack = ' '.join([
                aid,
                str(agent.get('description') or ''),
                ' '.join(map(str, agent.get('capabilities') or [])),
                ' '.join(map(str, agent.get('tags') or [])),
            ]).lower()
            score = sum(1 for token in goal_tokens if token and token in haystack)
            if best is None or score > best[0]:
                best = (score, agent)
        if best is None:
            return {
                'action': 'finish',
                'reason': 'No unused Agent remains.',
                'final_result': history[-1].get('output') if history else {},
                '_supervisor': {'backend': 'deterministic'},
            }
        agent = best[1]
        return {
            'action': 'run_agent',
            'agent_id': agent.get('agent_id') or agent.get('name'),
            'reason': 'Deterministic development supervisor selected the next matching unused Agent.',
            'input': {'goal': goal, 'previous_steps': history[-3:]},
            '_supervisor': {'backend': 'deterministic'},
        }

    def _llm(self, goal: str, initial_input: Any, agents: list[dict[str, Any]], history: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.base_url or not self.model:
            raise RuntimeError('LLM supervisor is not configured. Set SENDA_STUDIO_LLM_BASE_URL and SENDA_STUDIO_LLM_MODEL.')

        system = (
            'You are the Senda Argus multi-Agent supervisor. Select exactly one next action. '
            'You may only choose an agent_id present in the provided catalog. '
            'Do not invent tools or Agent IDs. Prefer finishing when the goal is already satisfied. '
            'Treat the Goal as the user orchestration prompt and use Agent descriptions/capabilities as routing metadata. '
            'Return JSON only with one of these forms: '
            '{"action":"run_agent","agent_id":"...","reason":"...","input":{...}} or '
            '{"action":"finish","reason":"...","final_result":...}. '
            'Keep Agent input minimal and derived from the goal, initial input, and previous Agent outputs.'
        )
        user_payload = {
            'goal': goal,
            'initial_input': _truncate(initial_input),
            'available_agents': _agent_catalog(agents),
            'history': _truncate(history),
        }
        payload = {
            'model': self.model,
            'temperature': 0,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': json.dumps(user_payload, ensure_ascii=False, default=str)},
            ],
        }
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        url = self.base_url + '/v1/chat/completions'
        started = time.perf_counter()
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode())
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        content = (((data.get('choices') or [{}])[0].get('message') or {}).get('content'))
        result = _json_from_text(str(content or ''))
        result['_supervisor'] = {
            'backend': 'llm',
            'model': self.model,
            'latency_ms': latency_ms,
        }
        return result

    def _jev_state(self, goal: str, initial_input: Any, agents: list[dict[str, Any]], history: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            'goal': goal,
            'initial_input': _truncate(initial_input, 6000),
            'available_agents': _agent_catalog(agents),
            'workflow_history': _truncate(history[-8:], 10000),
        }

    def _jev_system_one(self, state: dict[str, Any], criteria: dict[str, Any]) -> dict[str, Any]:
        try:
            from typesafe_sdk import Choice, TypeSafeClient
        except Exception as exc:
            raise RuntimeError('Jev mode requires typesafe-sdk in the Agent Studio image') from exc
        if not self.jev_api_key:
            raise RuntimeError('Jev supervisor is not configured. Set TYPESAFE_API_KEY.')

        kwargs: dict[str, Any] = {'api_key': self.jev_api_key}
        if self.jev_base_url:
            # Current SDK accepts base_url; older 0.7.x builds may expose baseURL.
            kwargs['base_url'] = self.jev_base_url
        if self.jev_model:
            kwargs['model'] = self.jev_model
        kwargs['timeout'] = self.jev_timeout

        try:
            client = TypeSafeClient(**kwargs)
        except TypeError:
            # Compatibility with SDK variants using default_model/baseURL naming.
            kwargs2 = {'api_key': self.jev_api_key, 'timeout': self.jev_timeout}
            if self.jev_base_url:
                kwargs2['base_url'] = self.jev_base_url
            if self.jev_model:
                kwargs2['default_model'] = self.jev_model
            client = TypeSafeClient(**kwargs2)

        started = time.perf_counter()
        with client:
            response = client.system_one(
                state=state,
                questions={
                    'next_action': Choice(
                        instructions='Which worker Agent should run next to best advance the workflow Goal, or should the workflow finish?',
                        criteria=criteria,
                    ),
                },
                model=self.jev_model or None,
            )
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        answer = response.choices['next_action']
        return {
            'choice': str(answer.choice),
            'confidence': float(answer.confidence),
            'probabilities': {str(k): float(v) for k, v in dict(answer.probabilities).items()},
            'model': str(getattr(response, 'model', self.jev_model or 'jev-latest')),
            'request_id': getattr(response, 'request_id', None),
            'latency_ms': latency_ms,
            'usage': {
                'input_tokens': getattr(getattr(response, 'usage', None), 'input_tokens', None),
                'output_tokens': getattr(getattr(response, 'usage', None), 'output_tokens', None),
            },
        }

    def _jev(self, goal: str, initial_input: Any, agents: list[dict[str, Any]], history: list[dict[str, Any]]) -> dict[str, Any]:
        if len(agents) > 254:
            raise RuntimeError('Jev Choice routing supports at most 254 callable Agents plus the finish option')

        criteria: dict[str, Any] = {}
        for a in agents:
            aid = str(a.get('agent_id') or '')
            if not aid:
                continue
            criteria[aid] = {
                'description': a.get('description', ''),
                'capabilities': a.get('capabilities', []),
                'tags': a.get('tags', []),
                'risk_level': a.get('risk_level', 'low'),
                'requires_approval': bool(a.get('requires_approval')),
            }
        finish_key = '__finish__'
        criteria[finish_key] = 'The workflow Goal is already satisfied; no additional worker Agent is needed.'
        state = self._jev_state(goal, initial_input, agents, history)
        telemetry = self._jev_system_one(state, criteria)

        choice = telemetry['choice']
        confidence = float(telemetry.get('confidence') or 0)
        supervisor = {
            'backend': 'jev',
            'model': telemetry.get('model'),
            'selected': choice,
            'confidence': confidence,
            'probabilities': telemetry.get('probabilities') or {},
            'request_id': telemetry.get('request_id'),
            'latency_ms': telemetry.get('latency_ms'),
            'usage': telemetry.get('usage') or {},
            'confidence_threshold': self.jev_confidence_threshold,
        }

        if self.jev_confidence_threshold > 0 and confidence < self.jev_confidence_threshold and self.jev_fallback == 'llm':
            fallback = self._llm(goal, initial_input, agents, history)
            fallback.setdefault('_supervisor', {})['fallback_from'] = supervisor
            return fallback

        if choice == finish_key:
            result = history[-1].get('output') if history else initial_input
            return {
                'action': 'finish',
                'reason': f'Jev selected finish with confidence {confidence:.3f}.',
                'final_result': result,
                '_supervisor': supervisor,
            }

        valid_ids = {str(a.get('agent_id')) for a in agents}
        if choice not in valid_ids:
            raise ValueError(f'Jev selected unregistered or disallowed Agent: {choice!r}')

        previous_outputs = [
            {'agent_id': s.get('agent_id'), 'output': s.get('output')}
            for s in history[-5:]
            if s.get('action') == 'run_agent'
        ]
        return {
            'action': 'run_agent',
            'agent_id': choice,
            'reason': f'Jev selected {choice} with confidence {confidence:.3f}.',
            'input': {
                'goal': goal,
                'initial_input': initial_input,
                'previous_outputs': previous_outputs,
                'supervisor_decision': {
                    'backend': 'jev',
                    'selected_agent': choice,
                    'confidence': confidence,
                },
            },
            '_supervisor': supervisor,
        }

    def plan(self, goal: str, initial_input: Any, agents: list[dict[str, Any]], history: list[dict[str, Any]]) -> dict[str, Any]:
        if self.mode == 'deterministic':
            result = self._deterministic(goal, agents, history)
        elif self.mode == 'jev':
            result = self._jev(goal, initial_input, agents, history)
        elif self.mode == 'llm':
            result = self._llm(goal, initial_input, agents, history)
        else:
            raise ValueError(f'Unsupported supervisor mode: {self.mode!r}')

        action = result.get('action')
        if action not in {'run_agent', 'finish'}:
            raise ValueError(f'Unsupported supervisor action: {action!r}')
        if action == 'run_agent':
            valid_ids = {str(a.get('agent_id')) for a in agents}
            if str(result.get('agent_id')) not in valid_ids:
                raise ValueError(f'Supervisor selected unregistered or disallowed Agent: {result.get("agent_id")!r}')
            if not isinstance(result.get('input'), dict):
                result['input'] = {'value': result.get('input')}
        return result


# Backward compatible class name used by earlier tests/imports.
LLMPlanner = SupervisorPlanner


class WorkflowOrchestrator:
    def __init__(self, store: EventStore, runtime: DockerRuntime, events: EventBus | None = None):
        self.store = store
        self.runtime = runtime
        self.events = events or EventBus(store)

    async def _event(self, workflow_id: str, event_type: str, status: str, **payload: Any) -> None:
        ev = {
            'event_id': 'evt_' + uuid.uuid4().hex,
            'timestamp': _utcnow(),
            'agent_id': SUPERVISOR_AGENT_ID,
            'run_id': workflow_id,
            'trace_id': workflow_id,
            'event_type': event_type,
            'status': status,
            'workflow_id': workflow_id,
            **payload,
        }
        await self.events.publish([ev])

    def _candidate_agents(self, workflow: dict[str, Any]) -> list[dict[str, Any]]:
        agents = self.store.merge_agent_profiles(self.runtime.list_agents())
        allowed = set(workflow.get('allowed_agents') or [])
        if allowed:
            agents = [a for a in agents if a.get('agent_id') in allowed]
        out = []
        supervisor_callers = {SUPERVISOR_AGENT_ID, LEGACY_ORCHESTRATOR_AGENT_ID}
        for a in agents:
            callers = set(a.get('allowed_callers') or [])
            if callers and not callers.intersection(supervisor_callers):
                continue
            out.append(a)
        return out

    async def _execute_agent_step(self, workflow: dict[str, Any], step_no: int, agent_id: str, reason: str, input_data: dict[str, Any]) -> Any:
        profile = self.store.get_agent_profile(agent_id)
        if profile.get('requires_approval'):
            self.store.add_workflow_step(workflow['workflow_id'], step_no, 'run_agent', 'pending_approval', agent_id=agent_id, reason=reason, input_data=input_data)
            self.store.update_workflow(workflow['workflow_id'], status='pending_approval', current_step=step_no, pending_agent_id=agent_id, pending_input={'reason': reason, 'input': input_data})
            await self._event(workflow['workflow_id'], 'workflow.approval.required', 'pending', agent_id=agent_id, reason=reason)
            return None
        return await self._run_agent_now(workflow, step_no, agent_id, reason, input_data)

    async def _run_agent_now(self, workflow: dict[str, Any], step_no: int, agent_id: str, reason: str, input_data: dict[str, Any]) -> Any:
        agent_run_id = 'ar_' + uuid.uuid4().hex[:20]
        self.store.add_workflow_step(workflow['workflow_id'], step_no, 'run_agent', 'running', agent_id=agent_id, agent_run_id=agent_run_id, reason=reason, input_data=input_data)
        self.store.update_workflow(workflow['workflow_id'], status='running', current_step=step_no, pending_agent_id=None, pending_input=None)
        await self._event(workflow['workflow_id'], 'workflow.agent.started', 'start', agent_id=agent_id, agent_run_id=agent_run_id, step_no=step_no, reason=reason)
        try:
            await asyncio.to_thread(self.runtime.run_agent, agent_id, agent_run_id, input_data, workflow['workflow_id'], SUPERVISOR_AGENT_ID)
            timeout = float((workflow.get('llm_config') or {}).get('agent_timeout', 300))
            state = await asyncio.to_thread(self.runtime.wait_run, agent_run_id, timeout)
            result = await asyncio.to_thread(self.runtime.run_result, agent_run_id)
            ok = int(state.get('exit_code') or 0) == 0
            self.store.update_workflow_step(workflow['workflow_id'], step_no, status='success' if ok else 'failed', output=result.get('result'))
            await self._event(
                workflow['workflow_id'],
                'workflow.agent.completed' if ok else 'workflow.agent.failed',
                'success' if ok else 'failed',
                agent_id=agent_id,
                agent_run_id=agent_run_id,
                step_no=step_no,
                exit_code=state.get('exit_code'),
                result=_truncate(result.get('result')),
            )
            if not ok:
                raise RuntimeError(f'Agent {agent_id} exited with code {state.get("exit_code")}')
            return result.get('result')
        except Exception as exc:
            try:
                self.store.update_workflow_step(workflow['workflow_id'], step_no, status='failed', output={'error': str(exc)})
            except Exception:
                pass
            raise

    async def run(self, workflow_id: str, approved_pending: bool = False) -> None:
        try:
            workflow = self.store.get_workflow(workflow_id)
            if workflow.get('status') in {'success', 'failed', 'cancelled'}:
                return
            self.store.update_workflow(workflow_id, status='running', error=None)
            await self._event(self.store.get_workflow(workflow_id)['workflow_id'], 'workflow.started', 'start', goal=workflow['goal'], supervisor_id=SUPERVISOR_AGENT_ID, supervisor_mode=(workflow.get('llm_config') or {}).get('mode', 'llm'))

            history = workflow.get('steps') or []
            step_no = int(workflow.get('current_step') or 0)

            if approved_pending and workflow.get('pending_agent_id'):
                pending = workflow.get('pending_input') or {}
                agent_id = workflow['pending_agent_id']
                reason = str(pending.get('reason') or 'User approved Agent execution.')
                input_data = pending.get('input') or {}
                await self._run_agent_now(workflow, step_no, agent_id, reason, input_data)
                history = self.store.workflow_steps(workflow_id)
                step_no += 1
                workflow = self.store.get_workflow(workflow_id)

            # Backward-compatible first-worker override. Normal workflows leave
            # this empty and the Supervisor selects the first worker from Goal + registry.
            if not history and workflow.get('entry_agent_id'):
                agent_id = workflow['entry_agent_id']
                reason = 'Configured first worker override (legacy entry_agent_id).'
                input_data = workflow.get('initial_input') or {}
                output = await self._execute_agent_step(workflow, step_no, agent_id, reason, input_data)
                if output is None and self.store.get_workflow(workflow_id).get('status') == 'pending_approval':
                    return
                step_no += 1
                history = self.store.workflow_steps(workflow_id)
                workflow = self.store.get_workflow(workflow_id)

            while step_no < int(workflow.get('max_steps') or 8):
                agents = self._candidate_agents(workflow)
                if not agents:
                    raise RuntimeError('No callable registered Agents are available for this workflow')
                planner = SupervisorPlanner(workflow.get('llm_config') or {})
                mode = planner.mode
                await self._event(workflow_id, 'supervisor.decision.requested', 'start', step_no=step_no, backend=mode, available_agent_ids=[a.get('agent_id') for a in agents], goal=_truncate(workflow['goal']))
                await self._event(workflow_id, 'orchestrator.plan.requested', 'start', step_no=step_no, backend=mode)
                if mode == 'jev':
                    await self._event(workflow_id, 'orchestrator.jev.requested', 'start', step_no=step_no, model=planner.jev_model, available_agent_ids=[a.get('agent_id') for a in agents])
                elif mode == 'llm':
                    await self._event(workflow_id, 'orchestrator.llm.requested', 'start', step_no=step_no, model=planner.model)

                try:
                    decision = await asyncio.to_thread(planner.plan, workflow['goal'], workflow.get('initial_input') or {}, agents, history)
                except Exception as exc:
                    await self._event(workflow_id, 'supervisor.decision.failed', 'failed', step_no=step_no, backend=mode, error=str(exc))
                    if mode == 'jev':
                        await self._event(workflow_id, 'orchestrator.jev.failed', 'failed', step_no=step_no, model=planner.jev_model, error=str(exc))
                    elif mode == 'llm':
                        await self._event(workflow_id, 'orchestrator.llm.failed', 'failed', step_no=step_no, model=planner.model, error=str(exc))
                    raise
                telemetry = decision.get('_supervisor') or {}
                public_decision = {k: v for k, v in decision.items() if k != '_supervisor'}

                await self._event(workflow_id, 'supervisor.decision.completed', 'success', step_no=step_no, backend=telemetry.get('backend', mode), decision=_truncate(public_decision), telemetry=_truncate(telemetry), model=telemetry.get('model'), latency_ms=telemetry.get('latency_ms'))
                await self._event(workflow_id, 'orchestrator.plan.completed', 'success', step_no=step_no, backend=telemetry.get('backend', mode), decision=_truncate(public_decision), telemetry=_truncate(telemetry))
                if telemetry.get('backend') == 'jev':
                    await self._event(
                        workflow_id,
                        'orchestrator.jev.completed',
                        'success',
                        step_no=step_no,
                        model=telemetry.get('model'),
                        selected_agent=telemetry.get('selected'),
                        confidence=telemetry.get('confidence'),
                        probabilities=_truncate(telemetry.get('probabilities') or {}),
                        request_id=telemetry.get('request_id'),
                        latency_ms=telemetry.get('latency_ms'),
                        usage=telemetry.get('usage') or {},
                    )
                elif telemetry.get('backend') == 'llm':
                    await self._event(workflow_id, 'orchestrator.llm.completed', 'success', step_no=step_no, model=telemetry.get('model'), latency_ms=telemetry.get('latency_ms'), decision=_truncate(public_decision))

                if decision.get('action') == 'finish':
                    result = decision.get('final_result')
                    self.store.add_workflow_step(workflow_id, step_no, 'finish', 'success', reason=str(decision.get('reason') or ''), output_data=result)
                    self.store.update_workflow(workflow_id, status='success', current_step=step_no, result=result, error=None)
                    await self._event(workflow_id, 'workflow.completed', 'success', result=_truncate(result), reason=decision.get('reason'))
                    return

                agent_id = str(decision.get('agent_id') or '')
                reason = str(decision.get('reason') or '')
                input_data = decision.get('input') or {}
                output = await self._execute_agent_step(workflow, step_no, agent_id, reason, input_data)
                if output is None and self.store.get_workflow(workflow_id).get('status') == 'pending_approval':
                    return
                step_no += 1
                history = self.store.workflow_steps(workflow_id)
                workflow = self.store.get_workflow(workflow_id)

            raise RuntimeError(f'Workflow reached max_steps={workflow.get("max_steps")} without finish action')
        except asyncio.CancelledError:
            try:
                self.store.update_workflow(workflow_id, status='stopped', error=None)
                await self._event(workflow_id, 'workflow.stopped', 'stopped')
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                self.store.update_workflow(workflow_id, status='failed', error=str(exc))
                await self._event(workflow_id, 'workflow.failed', 'failed', error=str(exc))
            except Exception:
                pass

    async def approve(self, workflow_id: str) -> None:
        wf = self.store.get_workflow(workflow_id)
        if wf.get('status') != 'pending_approval' or not wf.get('pending_agent_id'):
            raise ValueError('Workflow is not waiting for approval')
        await self._event(workflow_id, 'workflow.approval.granted', 'success', agent_id=wf.get('pending_agent_id'))
        await self.run(workflow_id, approved_pending=True)
