version 1.0

# Bad WDL syntax; won't run
workflow bad_workflow {
    input {
        type x
    }

    output {
        type y = x + 1
    }
}