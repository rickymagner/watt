version 1.0

workflow extract_stat {
    input {
        File table
    }

    call ParseFile {
        input:
            table=table
    }

    output {
        String name = ParseFile.name
        Float stat = ParseFile.stat
        Array[String] entries = ParseFile.entries
        Array[Array[String]] wdl_table = ParseFile.wdl_table
    }
}

task ParseFile {
    input {
        File table
    }

    command <<<
        SAMPLE_LINE=$(grep "sample" ~{table})
        if [ $? -eq 0 ]; then
            echo $SAMPLE_LINE | cut -d, -f2 > name.txt
            echo $SAMPLE_LINE | cut -d, -f3 > stat.txt
            cat "~{table}" | cut -d, -f1 > entries.txt
            cat "~{table}" | tr ',' '\t' > table.tsv
        else
            exit 1
        fi
    >>>

    output {
        String name = read_string("name.txt")
        Float stat = read_float("stat.txt")
        Array[String] entries = read_lines("entries.txt")
        Array[Array[String]] wdl_table = read_tsv("table.tsv")
    }
}