"""Run custom layers. Paper execution stays in finder_core through paper.py."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import re
from urllib.parse import urlsplit


class RunCancelled(Exception):
    pass


def text_field(value, name, limit, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f'{name} must be text with 1–{limit:,} characters.' if required else f'{name} is too long.')
    return value.strip()


def validate_connection(value):
    if not isinstance(value, dict) or value.get('mode') not in {'demo', 'live'}:
        raise ValueError('Choose Demo or Live API.')
    if value['mode'] == 'demo':
        return {'mode': 'demo'}
    endpoint = text_field(value.get('endpoint', ''), 'API address', 300).rstrip('/')
    address = urlsplit(endpoint)
    local = address.hostname in {'localhost', '127.0.0.1', '::1'}
    if address.scheme not in {'https', 'http'} or not address.hostname or address.username or address.password or address.query or address.fragment:
        raise ValueError('Use an API base URL without a password, query, or fragment.')
    if address.scheme == 'http' and not local:
        raise ValueError('Use HTTPS for a remote API. HTTP is only allowed on this computer.')
    key = text_field(value.get('api_key', ''), 'API key', 1000, required=not local)
    return {'mode': 'live', 'endpoint': endpoint, 'api_key': key}


def validate_workflow(value):
    if not isinstance(value, dict):
        raise ValueError('The workflow must be a JSON object.')
    name = text_field(value.get('name', 'My workflow'), 'Workflow name', 100)
    task = text_field(value.get('task', ''), 'Task and input', 100000)
    layers = value.get('layers')
    if not isinstance(layers, list) or not 1 <= len(layers) <= 6:
        raise ValueError('Use 1–6 layers.')
    clean_layers = []
    for index, layer in enumerate(layers, 1):
        if not isinstance(layer, dict):
            raise ValueError(f'Layer {index} is not an object.')
        mode = layer.get('mode', 'parallel')
        if mode not in {'parallel', 'sequential', 'debate'}:
            raise ValueError(f'Choose how agents work in layer {index}.')
        rounds = layer.get('rounds', 1) if mode == 'debate' else 1
        if type(rounds) is not int or not 1 <= rounds <= 5:
            raise ValueError('Use 1–5 debate rounds.')
        agents = layer.get('agents')
        if not isinstance(agents, list) or not 1 <= len(agents) <= 6:
            raise ValueError(f'Use 1–6 agents in layer {index}.')
        clean_agents = []
        for agent in agents:
            if not isinstance(agent, dict):
                raise ValueError('Each agent must have a role, model, and prompt.')
            model = text_field(agent.get('model', ''), 'Model ID', 150)
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:@+-]*', model):
                raise ValueError('Use the exact model ID from your provider.')
            clean_agents.append({'role': text_field(agent.get('role', ''), 'Role', 80), 'model': model, 'prompt': text_field(agent.get('prompt', ''), 'Role prompt', 12000)})
        clean_layers.append({'name': text_field(layer.get('name', f'Layer {index}'), 'Layer name', 100), 'mode': mode, 'rounds': rounds, 'agents': clean_agents})
    count = sum(len(layer['agents']) * layer['rounds'] for layer in clean_layers)
    if count > 80:
        raise ValueError('This workflow has more than 80 calls. Remove roles or rounds.')
    output = value.get('output', 'text')
    if output not in {'text', 'json'}:
        raise ValueError('Choose Text or JSON output.')
    schema = value.get('schema', {}) if output == 'json' else {}
    if output == 'json':
        from jsonschema import Draft202012Validator
        if not isinstance(schema, dict) or schema.get('type') != 'object' or len(json.dumps(schema)) > 12000:
            raise ValueError('Use a JSON schema with type "object" (up to 12,000 characters).')
        # Remote schema references could read network or local resources during validation.
        pending = [schema]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if '$ref' in item or '$dynamicRef' in item:
                    raise ValueError('Use an inline JSON schema without reference fields.')
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        try:
            Draft202012Validator.check_schema(schema)
        except Exception:
            raise ValueError('The JSON schema is not valid. Check its types and required fields.') from None
    max_tokens = value.get('max_tokens', 1500)
    if type(max_tokens) is not int or not 128 <= max_tokens <= 8000:
        raise ValueError('The output limit must be 128–8,000 tokens per call.')
    return {'name': name, 'task': task, 'layers': clean_layers, 'output': output, 'schema': schema, 'max_tokens': max_tokens}


def demo_value(schema):
    """Build basic sample JSON. Complex schemas may need a live run."""
    if 'const' in schema:
        return schema['const']
    if schema.get('enum'):
        return schema['enum'][0]
    kind = schema.get('type', 'string')
    if kind == 'object':
        return {key: demo_value(value) for key, value in schema.get('properties', {}).items()}
    if kind == 'array':
        return [demo_value(schema.get('items', {})) for _ in range(min(schema.get('minItems', 1), 20))]
    if kind in {'integer', 'number'}:
        value = max(0, schema.get('minimum', 0))
        if 'exclusiveMinimum' in schema:
            value = max(value, schema['exclusiveMinimum'] + 1)
        return min(value, schema.get('maximum', value))
    if kind == 'boolean':
        return False
    if kind == 'null':
        return None
    return 'Demo value'[:schema.get('maxLength', 100)]


def call_agent(agent, messages, connection, max_tokens, schema):
    if connection['mode'] == 'demo':
        if schema:
            return demo_value(schema)
        return f"Demo only — {agent['role']} received the task and {max(0, len(messages) - 2)} earlier messages. No evidence was analysed and no API was called."
    from openai import OpenAI
    prompt = messages
    if schema:
        prompt = [*messages, {'role': 'user', 'content': 'Return only a JSON object that follows this JSON schema: ' + json.dumps(schema)}]
    # No automatic retries: the user can see and control each paid attempt.
    with OpenAI(api_key=connection['api_key'] or 'local', base_url=connection['endpoint'], timeout=120, max_retries=0) as client:
        args = {'model': agent['model'], 'messages': prompt, 'max_completion_tokens': max_tokens}
        if schema:
            args['response_format'] = {'type': 'json_object'}
        response = client.chat.completions.create(**args)
    if not response.choices or not response.choices[0].message.content:
        raise ValueError('The provider returned no text. Check the model and output limit.')
    if response.choices[0].finish_reason == 'length':
        raise ValueError('The output limit was reached. Increase it before a new run.')
    content = response.choices[0].message.content
    if schema:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise ValueError('The provider did not return valid JSON. Check model support for JSON output.') from None
    return content


def run_custom(config, connection, progress, cancelled=None, caller=None):
    caller = caller or call_agent
    cancelled = cancelled or (lambda: False)
    records = []
    total = sum(len(layer['agents']) * layer['rounds'] for layer in config['layers'])

    def invoke(agent, history, layer_index, round_index, agent_index):
        if cancelled():
            raise RunCancelled()
        progress({'kind': 'started', 'role': agent['role'], 'layer': layer_index + 1, 'round': round_index + 1, 'agent': agent_index + 1, 'total': total})
        messages = [{'role': 'system', 'content': agent['prompt']}, {'role': 'user', 'content': config['task']}]
        messages.extend({'role': 'user', 'content': f"Earlier role: {row['role']}\n{json.dumps(row['output']) if isinstance(row['output'], dict) else row['output']}"} for row in history)
        if sum(len(message['content']) for message in messages) > 300000:
            raise ValueError('The shared context is too large. Use fewer roles, rounds, or input text.')
        schema = config['schema'] if config['output'] == 'json' and layer_index == len(config['layers']) - 1 else None
        output = caller(agent, messages, connection, config['max_tokens'], schema)
        if schema:
            from jsonschema import Draft202012Validator
            if next(Draft202012Validator(schema).iter_errors(output), None):
                raise ValueError('The final JSON does not match the schema. Demo supports basic schemas; a live model may need a clearer prompt.')
        record = {'layer': layer_index + 1, 'round': round_index + 1, 'agent': agent_index + 1, 'role': agent['role'], 'model': agent['model'], 'output': output}
        progress({'kind': 'completed', 'role': agent['role'], 'layer': layer_index + 1, 'round': round_index + 1, 'agent': agent_index + 1, 'total': total})
        return record

    for layer_index, layer in enumerate(config['layers']):
        if cancelled():
            raise RunCancelled()
        prior = list(records)
        if layer['mode'] == 'parallel':
            # Same-layer agents get identical history and do not see each other.
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(invoke, agent, prior, layer_index, 0, agent_index) for agent_index, agent in enumerate(layer['agents'])]
                records.extend(future.result() for future in futures)
        else:
            # Sequential and debate modes share each completed reply with the next role.
            for round_index in range(layer['rounds']):
                for agent_index, agent in enumerate(layer['agents']):
                    records.append(invoke(agent, list(records), layer_index, round_index, agent_index))
    if cancelled():
        raise RunCancelled()
    final_layer = len(config['layers'])
    final_round = config['layers'][-1]['rounds']
    outputs = [row for row in records if row['layer'] == final_layer and row['round'] == final_round]
    return {'kind': 'custom_experiment', 'mode': connection['mode'], 'paper_equivalent': False, 'created_at': datetime.now(timezone.utc).isoformat(), 'workflow': config, 'calls': len(records), 'outputs': outputs, 'trace': records}


def safe_error(error):
    """Provider errors can contain keys or input text. Never send their raw text."""
    if isinstance(error, ValueError):
        return str(error)
    name = type(error).__name__
    messages = {'AuthenticationError': 'The API rejected the key. Check the key and provider.', 'PermissionDeniedError': 'This key cannot use the selected model.', 'RateLimitError': 'The API limit was reached. Check your usage and try later.', 'APIConnectionError': 'Could not reach the API. Check the address and network.', 'APITimeoutError': 'The API took more than 120 seconds. No retry was made.', 'BadRequestError': 'The provider rejected the request. Check model support, JSON mode, and the output token limit.', 'ModuleNotFoundError': 'A package is missing. Install the packages in requirements.txt.'}
    return messages.get(name, 'The run stopped. Check the local input files, model, and settings before trying again.')
