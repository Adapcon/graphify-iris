Import Erp.Util

/// Sample IRIS class: a small persistent table stored in ^SAMPLEDATA.
Class Erp.SampleClass Extends (%Library.Persistent, Erp.SampleBase) [ ClassType = persistent, ProcedureBlock, StorageStrategy = SampleStorage ]
{

Parameter AUTHOR = "graphify";

Parameter TABLE = "Sample";

Index rowId On code [ IdKey, PrimaryKey ];

/// Record code
Property code As %Library.Integer(MAXVAL = 99, MINVAL = 1) [ Required ];

/// Record status
Property status As Erp.YesNo [ InitialExpression = 1 ];

Relationship items As Erp.SampleItem [ Cardinality = many, Inverse = sample ];

/// A collection property: `list` is IRIS syntax, the element class is the
/// reference. Written unqualified, so it resolves against this class's package.
Property lines As list Of SampleLine;

/// Store one record.
ClassMethod save(code As %Library.String, status As %Library.String) As %Library.Status
{
	new obj,sc
	set obj = ##class(Erp.SampleClass).%OpenId(code)
	if obj="" set obj = ##class(Erp.SampleClass).%New()
	set obj.code = code
	set sc = obj.%Save()
	if $$$ISERR(sc) quit sc
	do Audit^SAMPLEIRISRG(code)
	quit $$$OK
}

/// Validate the current record.
Method validate() As %Library.Status
{
	if ..status'=1 quit $$$ERROR(10000,"Invalid status")
	set ^SAMPLELOG($increment(^SAMPLELOG))=..code
	quit ..writeLog()
}

Method writeLog() As %Library.Status
{
	quit $$$OK
}

Storage SampleStorage
{
<DataLocation>^SAMPLEDATA</DataLocation>
<IdLocation>^SAMPLEDATA</IdLocation>
<Type>%Library.CacheSQLStorage</Type>
}

}
