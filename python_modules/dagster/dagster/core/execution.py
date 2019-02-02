'''
Naming conventions:

For public functions:

execute_*

These represent functions which do purely in-memory compute. They will evaluate expectations
the core transform, and exercise all logging and metrics tracking (outside of outputs), but they
will not invoke *any* outputs (and their APIs don't allow the user to).


'''

# too many lines
# pylint: disable=C0302

from collections import defaultdict, namedtuple
from contextlib import contextmanager
import inspect
import itertools
import sys

from future.utils import raise_from
from contextlib2 import ExitStack
from dagster import check
from dagster.utils import merge_dicts

from .definitions import (
    DEFAULT_OUTPUT,
    ContextCreationExecutionInfo,
    DependencyDefinition,
    PipelineDefinition,
    Solid,
    SolidInstance,
    solids_in_topological_order,
)

from .definitions.utils import check_opt_two_dim_str_dict
from .definitions.environment_configs import construct_environment_config, construct_context_config

from .execution_context import ExecutionContext, RuntimeExecutionContext, ExecutionMetadata

from .errors import (
    DagsterInvariantViolationError,
    DagsterUnmarshalInputNotFoundError,
    DagsterUnmarshalInputError,
    DagsterMarshalOutputError,
    DagsterMarshalOutputNotFoundError,
    DagsterExecutionStepNotFoundError,
)

from .events import construct_event_logger

from .execution_plan.create import ExecutionPlanSubsetInfo, create_execution_plan_core

from .execution_plan.objects import ExecutionPlan, ExecutionPlanInfo, StepResult, StepKind

from .execution_plan.simple_engine import execute_plan_core

from .system_config.objects import EnvironmentConfig

from .types.evaluator import EvaluationError, evaluate_config_value, friendly_string_for_error
from .types.marshal import FilePersistencePolicy


class PipelineExecutionResult(object):
    '''Result of execution of the whole pipeline. Returned eg by :py:func:`execute_pipeline`.

    Attributes:
        pipeline (PipelineDefinition): Pipeline that was executed
        context (ExecutionContext): ExecutionContext of that particular Pipeline run.
        result_list (list[SolidExecutionResult]): List of results for each pipeline solid.
    '''

    def __init__(self, pipeline, context, result_list):
        self.pipeline = check.inst_param(pipeline, 'pipeline', PipelineDefinition)
        self.context = check.inst_param(context, 'context', RuntimeExecutionContext)
        self.result_list = check.list_param(
            result_list, 'result_list', of_type=SolidExecutionResult
        )
        self.run_id = context.run_id

    @property
    def success(self):
        '''Whether the pipeline execution was successful at all steps'''
        return all([result.success for result in self.result_list])

    def result_for_solid(self, name):
        '''Get a :py:class:`SolidExecutionResult` for a given solid name.

        Returns:
          SolidExecutionResult
        '''
        check.str_param(name, 'name')

        if not self.pipeline.has_solid(name):
            raise DagsterInvariantViolationError(
                'Try to get result for solid {name} in {pipeline}. No such solid.'.format(
                    name=name, pipeline=self.pipeline.display_name
                )
            )

        for result in self.result_list:
            if result.solid.name == name:
                return result

        raise DagsterInvariantViolationError(
            'Did not find result for solid {name} in pipeline execution result'.format(name=name)
        )


class SolidExecutionResult(object):
    '''Execution result for one solid of the pipeline.

    Attributes:
      context (ExecutionContext): ExecutionContext of that particular Pipeline run.
      solid (SolidDefinition): Solid for which this result is
    '''

    def __init__(self, context, solid, step_results_by_kind):
        self.context = check.inst_param(context, 'context', RuntimeExecutionContext)
        self.solid = check.inst_param(solid, 'solid', Solid)
        self.step_results_by_kind = check.dict_param(
            step_results_by_kind, 'step_results_by_kind', key_type=StepKind, value_type=list
        )

    @property
    def transforms(self):
        return self.step_results_by_kind.get(StepKind.TRANSFORM, [])

    @property
    def input_expectations(self):
        return self.step_results_by_kind.get(StepKind.INPUT_EXPECTATION, [])

    @property
    def output_expectations(self):
        return self.step_results_by_kind.get(StepKind.OUTPUT_EXPECTATION, [])

    @staticmethod
    def from_results(context, results):
        check.inst_param(context, 'context', RuntimeExecutionContext)
        results = check.list_param(results, 'results', StepResult)
        if results:
            step_results_by_kind = defaultdict(list)

            solid = None
            for result in results:
                if solid is None:
                    solid = result.step.solid
                check.invariant(result.step.solid is solid, 'Must all be from same solid')

            for result in results:
                step_results_by_kind[result.kind].append(result)

            return SolidExecutionResult(
                context=context,
                solid=results[0].step.solid,
                step_results_by_kind=dict(step_results_by_kind),
            )
        else:
            check.failed("Cannot create SolidExecutionResult from empty list")

    @property
    def success(self):
        '''Whether the solid execution was successful'''
        return all(
            [
                result.success
                for result in itertools.chain(
                    self.input_expectations, self.output_expectations, self.transforms
                )
            ]
        )

    @property
    def transformed_values(self):
        '''Return dictionary of transformed results, with keys being output names.
        Returns None if execution result isn't a success.'''
        if self.success and self.transforms:
            return {
                result.success_data.output_name: result.success_data.value
                for result in self.transforms
            }
        else:
            return None

    def transformed_value(self, output_name=DEFAULT_OUTPUT):
        '''Returns transformed value either for DEFAULT_OUTPUT or for the output
        given as output_name. Returns None if execution result isn't a success'''
        check.str_param(output_name, 'output_name')

        if not self.solid.definition.has_output(output_name):
            raise DagsterInvariantViolationError(
                '{output_name} not defined in solid {solid}'.format(
                    output_name=output_name, solid=self.solid.name
                )
            )

        if self.success:
            for result in self.transforms:
                if result.success_data.output_name == output_name:
                    return result.success_data.value
            raise DagsterInvariantViolationError(
                (
                    'Did not find result {output_name} in solid {self.solid.name} '
                    'execution result'
                ).format(output_name=output_name, self=self)
            )
        else:
            return None

    @property
    def dagster_error(self):
        '''Returns exception that happened during this solid's execution, if any'''
        for result in itertools.chain(
            self.input_expectations, self.output_expectations, self.transforms
        ):
            if not result.success:
                return result.failure_data.dagster_error


def create_execution_plan(pipeline, env_config=None, execution_metadata=None, subset_info=None):
    check.inst_param(pipeline, 'pipeline', PipelineDefinition)
    check.opt_dict_param(env_config, 'env_config', key_type=str)
    execution_metadata = execution_metadata if execution_metadata else ExecutionMetadata()
    check.inst_param(execution_metadata, 'execution_metadata', ExecutionMetadata)
    check.opt_inst_param(subset_info, 'subset_info', ExecutionPlanSubsetInfo)

    typed_environment = create_typed_environment(pipeline, env_config)
    return create_execution_plan_with_typed_environment(
        pipeline, typed_environment, execution_metadata, subset_info
    )


def create_execution_plan_with_typed_environment(
    pipeline, typed_environment, execution_metadata, subset_info=None
):
    check.inst_param(pipeline, 'pipeline', PipelineDefinition)
    check.inst_param(typed_environment, 'environment', EnvironmentConfig)
    check.inst_param(execution_metadata, 'execution_metadata', ExecutionMetadata)
    check.opt_inst_param(subset_info, 'subset_info', ExecutionPlanSubsetInfo)

    with yield_context(pipeline, typed_environment, execution_metadata) as context:
        return create_execution_plan_core(
            ExecutionPlanInfo(context, pipeline, typed_environment), execution_metadata, subset_info
        )


def get_event_callback(reentrant_info):
    check.opt_inst_param(reentrant_info, 'reentrant_info', ExecutionMetadata)
    if reentrant_info and reentrant_info.event_callback:
        return check.callable_param(reentrant_info.event_callback, 'event_callback')
    else:
        return None


def get_tags(user_context_params, execution_metadata, pipeline):
    check.inst_param(user_context_params, 'user_context_params', ExecutionContext)
    check.opt_inst_param(execution_metadata, 'execution_metadata', ExecutionMetadata)
    check.inst_param(pipeline, 'pipeline', PipelineDefinition)

    base_tags = merge_dicts({'pipeline': pipeline.name}, user_context_params.tags)

    if execution_metadata and execution_metadata.tags:
        user_keys = set(user_context_params.tags.keys())
        provided_keys = set(execution_metadata.tags.keys())
        if not user_keys.isdisjoint(provided_keys):
            raise DagsterInvariantViolationError(
                (
                    'You have specified tags and user-defined tags '
                    'that overlap. User keys: {user_keys}. Reentrant keys: '
                    '{provided_keys}.'
                ).format(user_keys=user_keys, provided_keys=provided_keys)
            )

        return merge_dicts(base_tags, execution_metadata.tags)
    else:
        return base_tags


ResourceCreationInfo = namedtuple('ResourceCreationInfo', 'config run_id')


def _ensure_gen(thing_or_gen):
    if not inspect.isgenerator(thing_or_gen):

        def _gen_thing():
            yield thing_or_gen

        return _gen_thing()

    return thing_or_gen


@contextmanager
def with_maybe_gen(thing_or_gen):
    gen = _ensure_gen(thing_or_gen)

    try:
        thing = next(gen)
    except StopIteration:
        check.failed('Must yield one item. You did not yield anything.')

    yield thing

    stopped = False

    try:
        next(gen)
    except StopIteration:
        stopped = True

    check.invariant(stopped, 'Must yield one item. Yielded more than one item')


def _create_persistence_policy(persistence_config):
    check.dict_param(persistence_config, 'persistence_config', key_type=str)

    persistence_key, _config_value = list(persistence_config.items())[0]

    if persistence_key == 'file':
        return FilePersistencePolicy()
    else:
        check.failed('Unsupported persistence key: {}'.format(persistence_key))


@contextmanager
def yield_context(pipeline, environment, execution_metadata):
    check.inst_param(pipeline, 'pipeline', PipelineDefinition)
    check.inst_param(environment, 'environment', EnvironmentConfig)
    check.inst_param(execution_metadata, 'execution_metadata', ExecutionMetadata)

    context_definition = pipeline.context_definitions[environment.context.name]

    ec_or_gen = context_definition.context_fn(
        ContextCreationExecutionInfo(
            config=environment.context.config,
            pipeline_def=pipeline,
            run_id=execution_metadata.run_id,
        )
    )

    with with_maybe_gen(ec_or_gen) as execution_context:
        check.inst(execution_context, ExecutionContext)

        with _create_resources(
            pipeline, context_definition, environment, execution_context, execution_metadata.run_id
        ) as resources:
            loggers = _create_loggers(execution_metadata, execution_context)

            yield RuntimeExecutionContext(
                run_id=execution_metadata.run_id,
                loggers=loggers,
                resources=resources,
                tags=get_tags(execution_context, execution_metadata, pipeline),
                event_callback=get_event_callback(execution_metadata),
                environment_config=environment.original_config_dict,
                persistence_policy=_create_persistence_policy(environment.context.persistence),
            )


def _create_loggers(reentrant_info, execution_context):
    check.inst_param(reentrant_info, 'reentrant_info', ExecutionMetadata)
    check.inst_param(execution_context, 'execution_context', ExecutionContext)

    if reentrant_info and reentrant_info.event_callback:
        return execution_context.loggers + [construct_event_logger(reentrant_info.event_callback)]
    elif reentrant_info and reentrant_info.loggers:
        return execution_context.loggers + reentrant_info.loggers
    else:
        return execution_context.loggers


@contextmanager
def _create_resources(pipeline_def, context_def, environment, execution_context, run_id):
    if not context_def.resources:
        yield execution_context.resources
        return

    resources = {}
    check.invariant(
        not execution_context.resources,
        (
            'If resources explicitly specified on context definition, the context '
            'creation function should not return resources as a property of the '
            'ExecutionContext.'
        ),
    )

    # See https://bit.ly/2zIXyqw
    # The "ExitStack" allows one to stack up N context managers and then yield
    # something. We do this so that resources can cleanup after themselves. We
    # can potentially have many resources so we need to use this abstraction.
    with ExitStack() as stack:
        for resource_name in context_def.resources.keys():
            resource_obj_or_gen = get_resource_or_gen(
                context_def, resource_name, environment, run_id
            )

            resource_obj = stack.enter_context(with_maybe_gen(resource_obj_or_gen))

            resources[resource_name] = resource_obj

        context_name = environment.context.name

        resources_type = pipeline_def.context_definitions[context_name].resources_type
        yield resources_type(**resources)


def get_resource_or_gen(context_definition, resource_name, environment, run_id):
    resource_def = context_definition.resources[resource_name]
    # Need to do default values
    resource_config = environment.context.resources.get(resource_name, {}).get('config')
    return resource_def.resource_fn(ResourceCreationInfo(resource_config, run_id))


def _do_iterate_pipeline(
    pipeline, context, typed_environment, execution_metadata, throw_on_user_error=True
):
    check.inst(context, RuntimeExecutionContext)

    context.events.pipeline_start()

    execution_plan = create_execution_plan_core(
        ExecutionPlanInfo(context, pipeline, typed_environment), execution_metadata
    )

    steps = execution_plan.topological_steps()

    if not steps:
        context.debug(
            'Pipeline {pipeline} has no nodes and no execution will happen'.format(
                pipeline=pipeline.display_name
            )
        )
        context.events.pipeline_success()
        return

    context.debug(
        'About to execute the compute node graph in the following order {order}'.format(
            order=[step.key for step in steps]
        )
    )

    check.invariant(len(steps[0].step_inputs) == 0)

    pipeline_success = True

    for solid_result in _process_step_results(
        context, pipeline, execute_plan_core(context, execution_plan, throw_on_user_error)
    ):
        if not solid_result.success:
            pipeline_success = False
        yield solid_result

    if pipeline_success:
        context.events.pipeline_success()
    else:
        context.events.pipeline_failure()


def execute_pipeline_iterator(
    pipeline, environment=None, throw_on_user_error=True, execution_metadata=None, solid_subset=None
):
    '''Returns iterator that yields :py:class:`SolidExecutionResult` for each
    solid executed in the pipeline.

    This is intended to allow the caller to do things between each executed
    node. For the 'synchronous' API, see :py:func:`execute_pipeline`.

    Parameters:
      pipeline (PipelineDefinition): pipeline to run
      execution (ExecutionContext): execution context of the run
    '''
    check.inst_param(pipeline, 'pipeline', PipelineDefinition)
    check.opt_dict_param(environment, 'environment')
    check.bool_param(throw_on_user_error, 'throw_on_user_error')
    execution_metadata = execution_metadata if execution_metadata else ExecutionMetadata()
    check.inst_param(execution_metadata, 'execution_metadata', ExecutionMetadata)
    check.opt_list_param(solid_subset, 'solid_subset', of_type=str)

    pipeline_to_execute = get_subset_pipeline(pipeline, solid_subset)
    typed_environment = create_typed_environment(pipeline_to_execute, environment)

    with yield_context(pipeline_to_execute, typed_environment, execution_metadata) as context:
        for solid_result in _do_iterate_pipeline(
            pipeline_to_execute,
            context,
            typed_environment,
            execution_metadata=execution_metadata,
            throw_on_user_error=throw_on_user_error,
        ):
            yield solid_result


def _process_step_results(context, pipeline, step_results):
    check.inst_param(context, 'context', RuntimeExecutionContext)
    check.inst_param(pipeline, 'pipeline', PipelineDefinition)

    step_results_by_solid_name = defaultdict(list)
    for step_result in step_results:
        step_results_by_solid_name[step_result.step.solid.name].append(step_result)

    for topo_solid in solids_in_topological_order(pipeline):
        if topo_solid.name in step_results_by_solid_name:
            yield SolidExecutionResult.from_results(
                context, step_results_by_solid_name[topo_solid.name]
            )


class PipelineConfigEvaluationError(Exception):
    def __init__(self, pipeline, errors, config_value, *args, **kwargs):
        self.pipeline = check.inst_param(pipeline, 'pipeline', PipelineDefinition)
        self.errors = check.list_param(errors, 'errors', of_type=EvaluationError)
        self.config_value = config_value

        error_msg = 'Pipeline "{pipeline}" config errors:'.format(pipeline=pipeline.name)

        error_messages = []

        for i_error, error in enumerate(self.errors):
            error_message = friendly_string_for_error(error)
            error_messages.append(error_message)
            error_msg += '\n    Error {i_error}: {error_message}'.format(
                i_error=i_error + 1, error_message=error_message
            )

        self.message = error_msg
        self.error_messages = error_messages

        super(PipelineConfigEvaluationError, self).__init__(error_msg, *args, **kwargs)


def execute_externalized_plan(
    pipeline,
    execution_plan,
    step_keys,
    inputs_to_marshal=None,
    outputs_to_marshal=None,
    environment=None,
    execution_metadata=None,
    throw_on_user_error=True,
):
    check.inst_param(pipeline, 'pipeline', PipelineDefinition)
    check.inst_param(execution_plan, 'execution_plan', ExecutionPlan)
    check.list_param(step_keys, 'step_keys', of_type=str)
    inputs_to_marshal = check_opt_two_dim_str_dict(
        inputs_to_marshal, 'inputs_to_marshal', value_type=str
    )
    outputs_to_marshal = check.opt_dict_param(
        outputs_to_marshal, 'outputs_to_marshal', key_type=str, value_type=list
    )
    environment = check.opt_dict_param(environment, 'environment')
    check.opt_inst_param(execution_metadata, 'execution_metadata', ExecutionMetadata)

    typed_environment = create_typed_environment(pipeline, environment)
    with yield_context(pipeline, typed_environment, execution_metadata) as context:

        _check_inputs_to_marshal(execution_plan, inputs_to_marshal)

        _check_outputs_to_marshal(execution_plan, outputs_to_marshal)

        inputs = _unmarshal_inputs(context, inputs_to_marshal, execution_plan)

        execution_plan = create_execution_plan_core(
            ExecutionPlanInfo(context, pipeline, typed_environment),
            execution_metadata=execution_metadata,
            subset_info=ExecutionPlanSubsetInfo.with_input_values(
                included_step_keys=step_keys, inputs=inputs
            ),
        )

        results = list(
            execute_plan_core(context, execution_plan, throw_on_user_error=throw_on_user_error)
        )

        _marshal_outputs(context, results, outputs_to_marshal)

        return results


def _check_outputs_to_marshal(execution_plan, outputs_to_marshal):
    if outputs_to_marshal:
        for step_key, outputs_for_step in outputs_to_marshal.items():
            if not execution_plan.has_step(step_key):
                raise DagsterExecutionStepNotFoundError(step_key=step_key)
            step = execution_plan.get_step_by_key(step_key)
            for output in outputs_for_step:
                check.param_invariant(
                    set(output.keys()) == set(['output', 'path']),
                    'outputs_to_marshal',
                    'Output must be a dict with keys "output" and "path"',
                )

                output_name = output['output']
                if not step.has_step_output(output_name):
                    raise DagsterMarshalOutputNotFoundError(
                        'Execution step {step_key} does not have output {output}'.format(
                            step_key=step_key, output=output_name
                        ),
                        step_key=step_key,
                        output_name=output_name,
                    )


def _check_inputs_to_marshal(execution_plan, inputs_to_marshal):
    if inputs_to_marshal:
        for step_key, input_dict in inputs_to_marshal.items():
            if not execution_plan.has_step(step_key):
                raise DagsterExecutionStepNotFoundError(step_key=step_key)
            step = execution_plan.get_step_by_key(step_key)
            for input_name in input_dict.keys():
                if input_name not in step.step_input_dict:
                    raise DagsterUnmarshalInputNotFoundError(
                        'Input {input_name} does not exist in execution step {key}'.format(
                            input_name=input_name, key=step.key
                        ),
                        input_name=input_name,
                        step_key=step.key,
                    )


def _marshal_outputs(context, results, outputs_to_marshal):
    for result in results:
        step = result.step
        if not (result.success and step.key in outputs_to_marshal):
            continue

        for output in outputs_to_marshal[step.key]:
            output_name = output['output']
            if output['output'] != result.success_data.output_name:
                continue

            output_type = step.step_output_dict[output_name].runtime_type
            try:
                context.persistence_policy.write_value(
                    output_type.serialization_strategy, output['path'], result.success_data.value
                )
            except Exception as e:  # pylint: disable=broad-except
                raise_from(
                    DagsterMarshalOutputError(
                        'Error during the marshalling of output {output_name} in step {step_key}'.format(
                            output_name=output_name, step_key=step.key
                        ),
                        user_exception=e,
                        original_exc_info=sys.exc_info(),
                        output_name=output_name,
                        step_key=step.key,
                    ),
                    e,
                )


def _unmarshal_inputs(context, inputs_to_marshal, execution_plan):
    inputs = defaultdict(dict)
    for step_key, input_dict in inputs_to_marshal.items():
        step = execution_plan.get_step_by_key(step_key)
        for input_name, file_path in input_dict.items():
            check.invariant(input_name in step.step_input_dict, 'Previously checked')

            step_input = step.step_input_dict[input_name]
            input_type = step_input.runtime_type

            check.invariant(input_type.serialization_strategy)

            try:
                input_value = context.persistence_policy.read_value(
                    input_type.serialization_strategy, file_path
                )
            except Exception as e:  # pylint: disable=broad-except
                raise_from(
                    DagsterUnmarshalInputError(
                        (
                            'Error during the marshalling of input {input_name} in step '
                            '{step_key}'
                        ).format(input_name=input_name, step_key=step.key),
                        user_exception=e,
                        original_exc_info=sys.exc_info(),
                        input_name=input_name,
                        step_key=step.key,
                    ),
                    e,
                )

            inputs[step_key][input_name] = input_value
    return dict(inputs)


def execute_plan(
    pipeline, execution_plan, environment=None, execution_metadata=None, throw_on_user_error=True
):
    check.inst_param(pipeline, 'pipeline', PipelineDefinition)
    check.inst_param(execution_plan, 'execution_plan', ExecutionPlan)
    check.opt_dict_param(environment, 'environment')
    execution_metadata = execution_metadata if execution_metadata else ExecutionMetadata()
    check.opt_inst_param(execution_metadata, 'execution_metadata', ExecutionMetadata)

    typed_environment = create_typed_environment(pipeline, environment)
    with yield_context(pipeline, typed_environment, execution_metadata) as context:
        return list(
            execute_plan_core(context, execution_plan, throw_on_user_error=throw_on_user_error)
        )


def execute_pipeline(
    pipeline, environment=None, throw_on_user_error=True, execution_metadata=None, solid_subset=None
):
    '''
    "Synchronous" version of :py:func:`execute_pipeline_iterator`.

    Note: throw_on_user_error is very useful in testing contexts when not testing for error conditions

    Parameters:
      pipeline (PipelineDefinition): Pipeline to run
      environment (dict): The enviroment that parameterizes this run
      throw_on_user_error (bool):
        throw_on_user_error makes the function throw when an error is encoutered rather than returning
        the py:class:`SolidExecutionResult` in an error-state.


    Returns:
      PipelineExecutionResult
    '''

    check.inst_param(pipeline, 'pipeline', PipelineDefinition)
    check.opt_dict_param(environment, 'environment')
    check.bool_param(throw_on_user_error, 'throw_on_user_error')
    execution_metadata = execution_metadata if execution_metadata else ExecutionMetadata()
    check.inst_param(execution_metadata, 'execution_metadata', ExecutionMetadata)
    check.opt_list_param(solid_subset, 'solid_subset', of_type=str)

    pipeline_to_execute = get_subset_pipeline(pipeline, solid_subset)
    typed_environment = create_typed_environment(pipeline_to_execute, environment)
    return execute_pipeline_with_metadata(
        pipeline_to_execute,
        typed_environment,
        execution_metadata=execution_metadata,
        throw_on_user_error=throw_on_user_error,
    )


def _dep_key_of(solid):
    return SolidInstance(solid.definition.name, solid.name)


def build_sub_pipeline(pipeline_def, solid_names):
    '''
    Build a pipeline which is a subset of another pipeline.
    Only includes the solids which are in solid_names.
    '''

    check.inst_param(pipeline_def, 'pipeline_def', PipelineDefinition)
    check.list_param(solid_names, 'solid_names', of_type=str)

    solid_name_set = set(solid_names)
    solids = list(map(pipeline_def.solid_named, solid_names))
    deps = {_dep_key_of(solid): {} for solid in solids}

    def _out_handle_of_inp(input_handle):
        if pipeline_def.dependency_structure.has_dep(input_handle):
            output_handle = pipeline_def.dependency_structure.get_dep(input_handle)
            if output_handle.solid.name in solid_name_set:
                return output_handle
        return None

    for solid in solids:
        for input_handle in solid.input_handles():
            output_handle = _out_handle_of_inp(input_handle)
            if output_handle:
                deps[_dep_key_of(solid)][input_handle.input_def.name] = DependencyDefinition(
                    solid=output_handle.solid.name, output=output_handle.output_def.name
                )

    return PipelineDefinition(
        name=pipeline_def.name,
        solids=list({solid.definition for solid in solids}),
        context_definitions=pipeline_def.context_definitions,
        dependencies=deps,
    )


def execute_pipeline_with_metadata(
    pipeline, typed_environment, execution_metadata, throw_on_user_error
):
    check.inst_param(pipeline, 'pipeline', PipelineDefinition)
    check.inst_param(typed_environment, 'typed_environment', EnvironmentConfig)
    check.inst_param(execution_metadata, 'execution_metadata', ExecutionMetadata)

    with yield_context(pipeline, typed_environment, execution_metadata) as context:
        return PipelineExecutionResult(
            pipeline,
            context,
            list(
                _do_iterate_pipeline(
                    pipeline,
                    context,
                    typed_environment,
                    execution_metadata=execution_metadata,
                    throw_on_user_error=throw_on_user_error,
                )
            ),
        )


def get_subset_pipeline(pipeline, solid_subset):
    check.inst_param(pipeline, 'pipeline', PipelineDefinition)
    check.opt_list_param(solid_subset, 'solid_subset', of_type=str)
    return pipeline if solid_subset is None else build_sub_pipeline(pipeline, solid_subset)


def create_typed_environment(pipeline, environment=None):
    check.inst_param(pipeline, 'pipeline', PipelineDefinition)
    check.opt_dict_param(environment, 'environment')

    result = evaluate_config_value(pipeline.environment_type, environment)

    if not result.success:
        raise PipelineConfigEvaluationError(pipeline, result.errors, environment)

    return construct_environment_config(result.value)


def create_typed_context(pipeline, context=None):
    check.inst_param(pipeline, 'pipeline', PipelineDefinition)
    check.opt_dict_param(context, 'context')

    result = evaluate_config_value(pipeline.context_type, context)

    if not result.success:
        raise PipelineConfigEvaluationError(pipeline, result.errors, context)

    return construct_context_config(result.value['context'])


class ExecutionSelector(object):
    def __init__(self, name, solid_subset=None):
        self.name = check.str_param(name, 'name')
        if solid_subset is None:
            self.solid_subset = None
        else:
            self.solid_subset = check.opt_list_param(solid_subset, 'solid_subset', of_type=str)
