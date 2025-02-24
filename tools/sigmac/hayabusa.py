import copy
from collections import OrderedDict
from io import StringIO
import yaml
import re

from sigma.backends.base import SingleTextQueryBackend
from sigma.parser.condition import SigmaAggregationParser, ConditionOR, ConditionAND
from sigma.parser.modifiers.base import SigmaTypeModifier
from sigma.parser.modifiers.type import SigmaRegularExpressionModifier

SPECIAL_REGEX = re.compile("^\{(\d)+,?(\d+)?\}")


class HayabusaBackend(SingleTextQueryBackend):
    """Base class for backends that generate one text-based expression from a Sigma rule"""
    # see tools.py
    # use this value when sigmac parse argument of "-t"
    identifier = "hayabusa"
    active = True
    # the following class variables define the generation and behavior of queries from a parse tree some are prefilled with default values that are quite usual
    # Token used for linking expressions with logical AND
    andToken = " and "
    orToken = " or "                    # Same for OR
    notToken = " not "                  # Same for NOT
    # Syntax for subexpressions, usually parenthesis around it. %s is inner expression
    subExpression = "(%s)"
    valueExpression = "%s"              # Expression of values, %s represents value
    # Expression of typed values generated by type modifiers. modifier identifier -> expression dict, %s represents value
    typedValueExpression = dict()
    sort_condition_lists = False
    mapListsSpecialHandling = True
    name_idx = 1
    selection_prefix = "SELECTION_{0}"
    name_2_selection = OrderedDict()

    def __init__(self, sigmaconfig, options):
        super().__init__(sigmaconfig)
        self.re_init()

    def re_init(self):
        self.name_idx = 1
        self.name_2_selection = OrderedDict()

    def cleanValue(self, val):
        return val

    def generateListNode(self, node):
        return self.generateORNode(node)

    def create_new_selection(self):
        name = self.selection_prefix.format(self.name_idx)
        self.name_idx += 1
        return name

    def generateMapItemNode(self, node):
        fieldname, value = node
        transformed_fieldname = self.fieldNameMapping(fieldname, value)
        if self.mapListsSpecialHandling == False and type(value) in (str, int, list) or self.mapListsSpecialHandling == True and type(value) in (str, int):
            name = self.create_new_selection()
            self.name_2_selection[name] = [
                (transformed_fieldname, self.generateNode(value))]
            return name
        elif type(value) == list:
            return self.generateMapItemListNode(transformed_fieldname, value)
        elif isinstance(value, SigmaTypeModifier):
            return self.generateMapItemTypedNode(transformed_fieldname, value)
        elif value is None:
            # nullは正規表現で表す。これでいいのかちょっと不安
            return self.generateNode((transformed_fieldname+"|re", "^$"))
        else:
            raise TypeError(
                "Backend does not support map values of type " + str(type(value)))

    def generateMapItemTypedNode(self, fieldname, value):
        # `|re`オプションに対応
        if type(value) == SigmaRegularExpressionModifier:
            fieldname = fieldname + "|re"

            # pythonとかの正規表現では/(スラッシュ)や"(ダブルクオート)をエスケープしてもエラーが出ないが、Rustの正規表現エンジンではスラッシュやダブルクオートをエスケープするとエラーが出てしまう
            # そこでスラッシュやダブルクオートのエスケープは消しておく。
            # あと、この実装は結構怪しいので、将来バージョンではこの実装を無くして、hayabusa側で使用する正規表現エンジンを普通のpythonとかで使われているやつに変えた方がいいと思う。
            regex_value = value.value.replace('\/', '/')
            regex_value = regex_value.replace("\\\"", "\"")

            # 追加のケースとして、pythonとかの正規表現では{はエスケープ不要だが、Rustでは必要なので、それを修正するためのコード。めんどい
            idx = 0
            prev_regex = regex_value
            regex_value = ""
            while idx < len(prev_regex):
                # 既にエスケープされているものはスキップする。
                if prev_regex[idx:idx+2] == "\\{" or prev_regex[idx:idx+2] == "\\}":
                    regex_value += prev_regex[idx:idx+2]
                    idx += 2
                    continue

                ch = prev_regex[idx]
                # エスケープ不要な}はここに来ないように、以降の処理でidxを調整している。なのでここにくる}はエスケープが必要。
                if ch == "}":
                    regex_value += "\\}"
                    idx += 1
                    continue

                # {じゃない場合はそのまま足すだけ
                if ch != "{":
                    regex_value += ch
                    idx += 1
                    continue

                # {の場合の処理
                reg_match = SPECIAL_REGEX.match(prev_regex[idx:])
                if reg_match == None:
                    # 文字列としての{なので、エスケープ必要
                    regex_value += "\\{"
                    idx += 1
                else:
                    # これは桁数を指定する{なので、エスケープ不要で}までidxをスキップ
                    regex_value += reg_match.group()
                    idx += len(reg_match.group())

            return self.generateNode((fieldname, regex_value))
        else:
            raise NotImplementedError(
                "Type modifier '{}' is not supported by backend".format(value.identifier))

    def generateMapItemListNode(self, fieldname, value):
        # 下記のようなケースに対応
        # selection:
        # EventID:
        ###         - 1
        ###         - 2
        # 基本的にリストはORと良く、generateListNodeもORNodeを生成している。
        # しかし、上記のケースでgenerateListNode()を実行すると、下記のようなYAMLになってしまう。
        # selection:
        ###     EventID: 1 or 2
        # 上記のようにならないように、修正している。
        # なお、generateMapItemListNode()を有効にするために、self.mapListsSpecialHandling = Trueとしている
        if self._is_all_str(value):
            name = self.create_new_selection()
            self.name_2_selection[name] = [(fieldname, value)]
            return name

        list_values = list()
        for sub_node in value:
            list_values.append((fieldname, sub_node))
        return self.subExpression % self.generateORNode(list_values)

    def _is_all_str(self, values):
        for value in values:
            if type(value) != str:
                return False
        return True

    def generateAggregation(self, agg):
        # python3 tools/sigmac rules/windows/process_creation/win_dnscat2_powershell_implementation.yml --config tools/config/generic/sysmon.yml --target hayabusa
        if agg == None:
            return ""
        if agg.aggfunc == SigmaAggregationParser.AGGFUNC_COUNT:
            # condition の中に "|" は1つのみ
            # | 以降をそのまま出力する
            target = '|'
            condition = agg.parser.parsedyaml["detection"]["condition"]

            # conditionはなんと複数指定されることもあるらしい!!!!!
            # If multiple conditions are given, they are logically linked with OR.と仕様書に書いてある。詳細はSigmaRuleの仕様を参照のこと。
            # とりあえず、複数指定のconditionは未対応ということでエラーにするとして、(なお、デフォルトのbase.pyの実装で複数指定のconditionはexceptionがraiseされるので、そのような処理は追加で実装しなくてよい)
            # 問題となるのはagg.parser.parsedyaml["detection"]["condition"]の型
            ###
            # 下記のように指定すると、agg.parser.parsedyaml["detection"]["condition"]の型はstringになるが
            ### conditon: selection1
            ###
            # 下記のように指定すると、agg.parser.parsedyaml["detection"]["condition"]の型はlistになる
            # conditon:
            ###  - selection1
            ###
            # なのでlistのケースも想定して、下記のような実装とする。
            if type(condition) == list:
                condition = condition[0]
            index = condition.find(target)
            return condition[index:]
        # count以外は対応していないので、エラーを返す
        raise NotImplementedError(
            "This rule contains aggregation operator not implemented for this backend")

    def generateValueNode(self, node):
        # このメソッドをオーバーライドしておかないとint型もstr型として扱われてしまうので、int型やint型として、str型はstr型として処理するために実装した。
        # このメソッドは最悪無くてもいいような気もする。
        if type(node) == int:
            return node
        else:
            return self.valueExpression % (self.cleanValue(str(node)))

    # 全部strかどうかを判定
    def is_keyword_list(self, node):
        if type(node) != ConditionOR:
            return False

        for item in node.items:
            if type(item) != str:
                return False

        return True

    def generateANDNode(self, node):
        generated = list()
        for val in node:
            if type(val) == str or type(val) == int:
                # 普通はtupleでkeyとvalueのペアであるが、これはkeyが指定されていないケース
                # keyが指定されていない場合は、EventLog全体をgrep検索することになっている。(詳細はSigmaルールの仕様書を参照のこと)
                # 具体的には"all of"とか使うとこの分岐に来る
                name = self.create_new_selection()
                self.name_2_selection[name] = [(None, val)]
                generated_node = name
            else:
                # 普通はこっちにくる
                generated_node = self.generateNode(val)
            generated.append(generated_node)
        filtered = [g for g in generated if g is not None]
        if filtered:
            if self.sort_condition_lists:
                filtered = sorted(filtered)
            return self.andToken.join(filtered)
        else:
            return None

    def generateORNode(self, node):
        if self.is_keyword_list(node) == True:
            # 普通はtupleでkeyとvalueのペアであるが、これはkeyが指定されていないケース
            # 全てkeyが指定されていない場合はここに来る。
            name = self.create_new_selection()
            self.name_2_selection[name] = [(None, val) for val in node]
            return name

        name = None
        generated = list()
        for val in node:
            # 普通はtupleでkeyとvalueのペアであるが、これはkeyが指定されていないケース
            if type(val) == str or type(val) == int:
                if name is None:
                    name = self.create_new_selection()
                    self.name_2_selection[name] = list()
                self.name_2_selection[name].append((None, val))
            else:
                generated.append(self.generateNode(val))
        if name is not None:
            generated.append(name)

        filtered = [g for g in generated if g is not None]
        if filtered:
            if self.sort_condition_lists:
                filtered = sorted(filtered)
            return self.orToken.join(filtered)
        else:
            return None

    def generateQuery(self, parsed):
        # このクラスのインスタンスは再利用されるので、内部のメンバ変数をresetする。
        self.re_init()
        result = self.generateNode(parsed.parsedSearch)
        if parsed.parsedAgg:
            res = self.generateAggregation(parsed.parsedAgg)
            result += " " + res
        ret = ""
        with StringIO() as bs:
            # 元のyamlをいじるとこの後の処理に影響を与える可能性があるので、deepCopyする
            parsed_yaml = copy.deepcopy(parsed.sigmaParser.parsedyaml)
            # なんかタイトルは先頭に来てほしいので、そのための処理
            # parsed.sigmaParser.parsedyamlがOrderedDictならこんなことしなくていい、後で別のやり方があるか調べる
            # 順番固定してもいいかも
            bs.write("title: " + parsed_yaml["title"]+"\n")
            bs.write("ruletype: Sigma\n")
            del parsed_yaml["title"]
            
            # detectionの部分をクリアする前にtimeframeだけ確保しておく。
            timeframe = None
            if "timeframe" in parsed_yaml["detection"]:
                timeframe = parsed_yaml["detection"]["timeframe"]

            # detectionの部分だけ変更して出力する。
            parsed_yaml["detection"] = {}
            if timeframe is not None and len(timeframe) != 0:
                parsed_yaml["detection"]["timeframe"] = timeframe
            parsed_yaml["detection"]["condition"] = result
            for key, values in self.name_2_selection.items():
                # fieldnameの有無を確認している
                if values[0][0]:
                    # 通常はfieldnameがあってその場合は連想配列で初期化
                    parsed_yaml["detection"][key] = {}
                else:
                    # is_keyword_list() == Trueの場合だけ、ここにくる
                    parsed_yaml["detection"][key] = []

                for fieldname, value in values:
                    if fieldname == None:
                        ## is_keyword_list() == Trueの場合
                        parsed_yaml["detection"][key].append(value)
                    else:
                        ## is_keyword_list() == Falseの場合
                        parsed_yaml["detection"][key][fieldname] = value
                        
            yaml.dump(parsed_yaml, bs, indent=4, default_flow_style=False)
            ret = bs.getvalue()
            ret += "---\n"

        return ret
