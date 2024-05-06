version 1.0

workflow say_hello {
    input {
        String name
        Int num_times
        Boolean compress
    }

    call Announce {
        input:
            name=name,
            num_times=num_times,
            compress=compress
    }

    output {
        File announcement = Announce.announcement
    }
}

task Announce {
    input {
        String name
        Int num_times
        Boolean compress
    }

    command <<<
        for i in $(seq 1 ~{num_times})
        do
            echo "Hello ~{name}" >> announcement.txt
        done

        if ~{compress}; then
            gzip announcement.txt
        fi
    >>>

    output {
        File announcement = if compress then "announcement.txt.gz" else "announcement.txt"
    }
}