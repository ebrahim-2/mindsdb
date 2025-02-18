from typing import Iterable, List, Optional

import re
import hashlib


import pandas as pd
from mindsdb_sql import parse_sql
from mindsdb_sql.parser.ast import Identifier
from mindsdb_sql.planner.utils import query_traversal

from mindsdb.utilities import log
from mindsdb.utilities.context import context as ctx

logger = log.getLogger(__name__)


class SQLAgent:

    def __init__(
            self,
            command_executor,
            database: str,
            include_tables: Optional[List[str]] = None,
            ignore_tables: Optional[List[str]] = None,
            sample_rows_in_table_info: int = 3,
            cache: Optional[dict] = None
    ):
        self._database = database
        self._command_executor = command_executor

        self._sample_rows_in_table_info = int(sample_rows_in_table_info)

        self._tables_to_include = include_tables
        self._tables_to_ignore = []
        self._database = database
        if not self._tables_to_include:
            # ignore_tables and include_tables should not be used together.
            # include_tables takes priority if it's set.
            self._tables_to_ignore = ignore_tables or []
        self._cache = cache

    def _call_engine(self, query: str, database=None):
        # switch database

        ast_query = parse_sql(query.strip('`'))
        self._check_tables(ast_query)

        if database is None:
            database = self._database

        ret = self._command_executor.execute_command(
            ast_query,
            database_name=database
        )
        return ret

    def _check_tables(self, ast_query):

        def _check_f(node, is_table=None, **kwargs):
            if is_table and isinstance(node, Identifier):
                table = node.parts[-1]
                if table not in self._tables_to_include:
                    ValueError(f"Table {table} not found. Available tables: {', '.join(self._tables_to_include)}")

        query_traversal(ast_query, _check_f)

    def get_usable_table_names(self) -> Iterable[str]:

        cache_key = f'{ctx.company_id}_{self._database}_tables'

        # first check cache and return if found
        if self._cache:
            cached_tables = self._cache.get(cache_key)
            if cached_tables:
                return cached_tables

        if self._tables_to_include:
            return self._tables_to_include

        ret = self._call_engine('show databases;')
        dbs = [lst[0] for lst in ret.data.to_lists() if lst[0] != 'information_schema']
        usable_tables = []
        for db in dbs:
            if db != 'mindsdb' and db == self._database:
                try:
                    ret = self._call_engine('show tables', database=db)
                    tables = [lst[0] for lst in ret.data.to_lists() if lst[0] != 'information_schema']
                    for table in tables:
                        # By default, include all tables in a database unless expilcitly ignored.
                        table_name = f'{db}.{table}'
                        if table_name not in self._tables_to_ignore:
                            usable_tables.append(table_name)
                except Exception as e:
                    logger.warning('Unable to get tables for %s: %s', db, str(e))
        if self._cache:
            self._cache.set(cache_key, set(usable_tables))

        return usable_tables

    def _resolve_table_names(self, table_names: List[str], all_tables: List[Identifier]) -> List[Identifier]:
        """
        Tries to find table (which comes directly from an LLM) by its name
        Handles backticks (`) and tables without databases
        """

        # index to lookup table
        tables_idx = {}
        for table in all_tables:
            # by name
            tables_idx[(table.parts[-1],)] = table
            # by path
            tables_idx[tuple(table.parts)] = table

        tables = []
        for table_name in table_names:

            # Some LLMs (e.g. gpt-4o) may include backticks or quotes when invoking tools.
            table_name = table_name.strip(' `"\'\n\r')
            table = Identifier(table_name)

            # resolved table
            table2 = tables_idx.get(tuple(table.parts))

            if table2 is None:
                raise ValueError(f"Table {table} not found in database")
            tables.append(table2)

        return tables

    def get_table_info(self, table_names: Optional[List[str]] = None) -> str:
        """ Get information about specified tables.
        Follows best practices as specified in: Rajkumar et al, 2022 (https://arxiv.org/abs/2204.00498)
        If `sample_rows_in_table_info`, the specified number of sample rows will be
        appended to each table description. This can increase performance as demonstrated in the paper.
        """
        try:
            cache_key = self._generate_cache_key(table_names)
            tables_info = self._get_info_from_cache(cache_key, table_names)

            if not tables_info:
                tables_info = self._fetch_table_info(table_names)
                self._update_cache(cache_key, tables_info)

            return "\n\n".join(tables_info.values())
        except Exception as e:
            logger.error(f"Error fetching table info: {e}")
            return f"Error fetching table info: {e}"

    def _generate_cache_key(self, table_names: Optional[List[str]]) -> str:
        # Base part of the cache key
        base_key = f"{ctx.company_id}_{self._database}_table_info"

        # If table names are provided, sort and concatenate them
        if table_names:
            sorted_names = "_".join(sorted(table_names))
            full_key = f"{base_key}_{sorted_names}"
        else:
            full_key = base_key

        # Hash the full key to ensure a constant length
        hashed_key = hashlib.sha256(full_key.encode()).hexdigest()

        return hashed_key

    def _get_info_from_cache(self, cache_key: str, table_names: Optional[List[str]]) -> dict:
        """Retrieve table information from cache if available."""
        cached_info = self._cache.get(cache_key) if self._cache else None
        if cached_info and table_names:
            # Verify all requested tables are in cache
            missing_tables = set([name for name in table_names if name not in cached_info])
            if not missing_tables:
                return {name: cached_info[name] for name in table_names}
        return cached_info if not table_names else {}

    def _fetch_table_info(self, table_names: Optional[List[str]]) -> dict:
        """Fetch table information from the database."""
        all_tables = [Identifier(name) for name in self.get_usable_table_names()]

        if table_names is not None:
            all_tables = self._resolve_table_names(table_names, all_tables)

        tables_info = {}
        for table in all_tables:
            tables_info[table.parts[-1]] = self._get_single_table_info(table)
        return tables_info

    def _update_cache(self, cache_key: str, tables_info: dict) -> None:
        """Update the cache with the provided table information."""
        if self._cache:
            self._cache.set(cache_key, tables_info)

    def _get_single_table_info(self, table: Identifier) -> str:
        if len(table.parts) < 2:
            raise ValueError(f"Database is required for table: {table}")
        integration, table_name = table.parts[-2:]
        table_str = str(table)

        dn = self._command_executor.session.datahub.get(integration)

        fields, dtypes = [], []
        for column in dn.get_table_columns(table_name):
            fields.append(column['name'])
            dtypes.append(column.get('type', ''))

        info = f'Table named `{table_name}`\n'
        info += f"\n/* Sample with first {self._sample_rows_in_table_info} rows from table {table_str}:\n"
        info += "\t".join([field for field in fields])
        info += self._get_sample_rows(table_str, fields) + "\n*/"
        info += '\nColumn data types: ' + ",\t".join(
            [f'`{field}` : `{dtype}`' for field, dtype in zip(fields, dtypes)]) + '\n'  # noqa
        return info

    def _get_sample_rows(self, table: str, fields: List[str]) -> str:
        command = f"select {','.join(fields)} from {table} limit {self._sample_rows_in_table_info};"
        try:
            ret = self._call_engine(command)
            sample_rows = ret.data.to_lists()
            sample_rows = list(
                map(lambda ls: [str(i) if len(str(i)) < 100 else str[:100] + '...' for i in ls], sample_rows))
            sample_rows_str = "\n" + "\n".join(["\t".join(row) for row in sample_rows])
        except Exception as e:
            logger.warning(e)
            sample_rows_str = "\n" + "\t [error] Couldn't retrieve sample rows!"

        return sample_rows_str

    def _clean_query(self, query: str) -> str:
        # Sometimes LLM can input markdown into query tools.
        cmd = re.sub(r'```(sql)?', '', query)
        return cmd

    def query(self, command: str, fetch: str = "all") -> str:
        """Execute a SQL command and return a string representing the results.
        If the statement returns rows, a string of the results is returned.
        If the statement returns no rows, an empty string is returned.
        """

        def _tidy(result: List) -> str:
            return '\n'.join(['\t'.join([str(value) for value in row]) for row in result])

        def _repr_result(ret):
            limit_rows = 30

            columns_str = ', '.join([repr(col.name) for col in ret.columns])
            res = f'Output columns: {columns_str}\n'

            data = ret.to_lists()
            if len(data) > limit_rows:
                df = pd.DataFrame(data, columns=[col.name for col in ret.columns])

                res += f'Result has {len(data)} rows. Description of data:\n'
                res += str(df.describe(include='all')) + '\n\n'
                res += f'First {limit_rows} rows:\n'

            else:
                res += 'Result:\n'

            res += _tidy(data[:limit_rows])
            return res

        ret = self._call_engine(self._clean_query(command))
        if fetch == "all":
            result = _repr_result(ret.data)
        elif fetch == "one":
            result = _tidy(ret.data.to_lists()[0])
        else:
            raise ValueError("Fetch parameter must be either 'one' or 'all'")
        return str(result)

    def get_table_info_safe(self, table_names: Optional[List[str]] = None) -> str:
        try:
            return self.get_table_info(table_names)
        except Exception as e:
            return f"Error: {e}"

    def query_safe(self, command: str, fetch: str = "all") -> str:
        try:
            return self.query(command, fetch)
        except Exception as e:
            return f"Error: {e}"
